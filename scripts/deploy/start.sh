#!/bin/bash

set -e

cd "$(dirname "$0")/../.."

echo "====================================="
echo "Trinity Agent Platform - Starting"
echo "====================================="
echo ""

# --- Mode + pre-flight (#39: agent-driven one-shot install) -------------------
# Unattended/agent mode removes interactive hard-stops — required inputs are
# auto-generated-and-surfaced instead of prompting. An agent installing Trinity
# passes TRINITY_UNATTENDED=1 or --unattended so the happy path never blocks on
# a TTY. Whatever gets generated is echoed back in the final summary.
UNATTENDED="${TRINITY_UNATTENDED:-0}"
for _arg in "$@"; do [ "$_arg" = "--unattended" ] && UNATTENDED=1; done

# --- Hosted (pull-only) mode (#2280) -----------------------------------------
# `--hosted` runs the SAME install against prebuilt GHCR images instead of
# building from source: docker-compose.hosted.yml plus a pulled-and-retagged
# agent base image. It is a flag on this script rather than a second script on
# purpose — everything else an install needs (secret generation, the
# ADMIN_PASSWORD contract #2381 made honest, DOCKER_GID detection, the serving
# health poll, the next-steps card) is identical, and a parallel copy of it is
# precisely the shape that has gone stale here before (#1039, #1056, #1707,
# #1871). One code path, one behaviour, two image sources.
HOSTED="${TRINITY_HOSTED:-0}"
for _arg in "$@"; do [ "$_arg" = "--hosted" ] && HOSTED=1; done

# Compose file selection. Dev/default passes NO -f so `docker compose` merges
# docker-compose.yml + docker-compose.override.yml as it always has; hosted
# passes an explicit -f, which (deliberately, same as prod) disables that
# auto-merge.
COMPOSE_FILES=()
if [ "$HOSTED" = "1" ]; then
    COMPOSE_FILES=(-f docker-compose.hosted.yml)
fi
# The image tag every hosted pull resolves is settled AFTER .env exists — see
# resolve_image_tag() below. Resolving it here would be too early to read the
# file, which is the only place an --unattended or marketplace install can
# persist it.

# Fail fast with ONE consolidated, actionable message rather than crashing
# mid-run. Docker daemon + Compose v2 are hard requirements. `docker info` also
# doubles as the "daemon actually reachable" check the DOCKER_GID probe and
# `compose up` below both assume.
_preflight_problems=()
if ! docker info >/dev/null 2>&1; then
    _preflight_problems+=("Docker daemon not reachable — start Docker Desktop (macOS) or 'sudo systemctl start docker' (Linux), then re-run.")
fi
if ! docker compose version >/dev/null 2>&1; then
    _preflight_problems+=("Docker Compose v2 missing — upgrade Docker so the 'docker compose' plugin is present ('docker-compose' v1 is not supported).")
fi
if [ ${#_preflight_problems[@]} -gt 0 ]; then
    echo "❌ Pre-flight checks failed:" >&2
    for _p in "${_preflight_problems[@]}"; do echo "   • ${_p}" >&2; done
    echo "" >&2
    exit 1
fi

# Port conflicts are a WARNING, not a hard stop: on a re-run Trinity itself
# holds these ports (idempotent bring-up), so failing would break the common
# "start.sh again" case. `bash /dev/tcp` is portable (no lsof/ss/nc dependency);
# the redirect is guarded so `set -e` never trips on a free port.
_port_busy() { (exec 3<>"/dev/tcp/127.0.0.1/$1") >/dev/null 2>&1 && exec 3>&- 3<&- ; }
_busy_ports=()
for _pp in 80 8000 8080 6379; do _port_busy "$_pp" && _busy_ports+=("$_pp"); done
if [ ${#_busy_ports[@]} -gt 0 ]; then
    echo "ℹ️  Ports already in use: ${_busy_ports[*]}."
    echo "    If this is a re-run, that's expected — they're Trinity's own containers."
    echo "    If it's a fresh install and something else owns them, stop that service"
    echo "    (or set FRONTEND_PORT= in .env for the Web UI) before continuing."
    echo ""
fi
# -----------------------------------------------------------------------------

if [ ! -f .env ]; then
    echo "⚠️  No .env file found. Creating from template..."
    cp .env.example .env
    echo "✅ Created .env file. Please update with your configuration."
    echo ""
fi

# Auto-generate openssl-hex-32 secrets if blank.
# CREDENTIAL_ENCRYPTION_KEY, SECRET_KEY, and INTERNAL_API_SECRET are all
# 32-byte hex strings with no rotation story today — operator either has
# one or doesn't, and a fresh install needs one. Generating them on first
# boot is friendlier than the prior "boot, fail with a cryptic JWT error,
# go read the docs" path. (#443)
ensure_hex32_secret() {
    local var="$1"
    if grep -qE "^${var}=.+" .env 2>/dev/null; then
        return 0
    fi
    local val
    val=$(openssl rand -hex 32)
    if grep -qE "^${var}=$" .env 2>/dev/null; then
        sed -i.bak "s/^${var}=$/${var}=${val}/" .env && rm -f .env.bak
    else
        echo "${var}=${val}" >> .env
    fi
    echo "Auto-generated ${var}"
}

ensure_hex32_secret CREDENTIAL_ENCRYPTION_KEY
ensure_hex32_secret SECRET_KEY
ensure_hex32_secret INTERNAL_API_SECRET
# AGENT_AUTH_SECRET (#1159): stable master from which the backend derives each
# agent's in-container auth token. Same persist-once, never-rotate contract as
# the three above — once set it must not change, or every agent's token would
# shift and the running fleet would 401 until recreated.
ensure_hex32_secret AGENT_AUTH_SECRET

# ADMIN_PASSWORD has no sensible default. Interactively we fail fast rather than
# boot into a state the operator can't log into (#443). Unattended (#39), we
# generate a strong one and surface it in the final summary so an agent-run
# install never hard-stops on a TTY.
GENERATED_ADMIN_PASSWORD=""
if ! grep -qE '^ADMIN_PASSWORD=.+' .env 2>/dev/null; then
    if [ "$UNATTENDED" = "1" ]; then
        GENERATED_ADMIN_PASSWORD=$(openssl rand -base64 18 | tr -dc 'A-Za-z0-9' | head -c 24)
        if grep -qE '^ADMIN_PASSWORD=$' .env 2>/dev/null; then
            sed -i.bak "s|^ADMIN_PASSWORD=$|ADMIN_PASSWORD=${GENERATED_ADMIN_PASSWORD}|" .env && rm -f .env.bak
        else
            echo "ADMIN_PASSWORD=${GENERATED_ADMIN_PASSWORD}" >> .env
        fi
        echo "Auto-generated ADMIN_PASSWORD (unattended) — shown in the summary below."
    else
        cat >&2 <<EOF

ERROR: ADMIN_PASSWORD is blank in .env.
       Choose a strong password (12+ chars; the backend will reject
       weak defaults like "password" or "admin"), then re-run start.sh.
       For an unattended/agent-run install, pass --unattended (or set
       TRINITY_UNATTENDED=1) and one will be generated and printed for you.

EOF
        exit 1
    fi
fi

# A model API key isn't required to BOOT (the stack starts fine), but agents
# can't run without one. Warn now and surface it in the summary rather than let
# the user discover it only when their first agent fails. (#39)
MODEL_KEY_MISSING=0
if ! grep -qE '^(ANTHROPIC_API_KEY|GOOGLE_API_KEY|CLAUDE_CODE_OAUTH_TOKEN)=.+' .env 2>/dev/null; then
    MODEL_KEY_MISSING=1
fi

# Issue #589 — Redis passwords are mandatory.
# On fresh installs (no redis-data volume), generate them automatically.
# On existing deployments with data, refuse and point at the migration doc:
# re-keying a populated Redis would lock the backend out of its own data.
volume_exists() {
    docker volume inspect "$(basename "$PWD")_redis-data" >/dev/null 2>&1 \
        || docker volume inspect redis-data >/dev/null 2>&1
}

ensure_redis_passwords() {
    local missing=()
    grep -qE '^REDIS_PASSWORD=.+'         .env 2>/dev/null || missing+=(REDIS_PASSWORD)
    grep -qE '^REDIS_BACKEND_PASSWORD=.+' .env 2>/dev/null || missing+=(REDIS_BACKEND_PASSWORD)
    if [ ${#missing[@]} -eq 0 ]; then
        return 0
    fi

    if volume_exists; then
        cat >&2 <<EOF

ERROR: Redis volume already exists but ${missing[*]} is/are missing from .env.
       Re-keying a populated Redis will lock the backend out of its own data.
       See docs/migrations/REDIS_AUTH.md for the upgrade path.

EOF
        return 1
    fi

    echo "Generating Redis passwords (fresh install)..."
    for var in "${missing[@]}"; do
        if grep -qE "^${var}=$" .env 2>/dev/null; then
            sed -i.bak "s/^${var}=$/${var}=$(openssl rand -hex 24)/" .env && rm -f .env.bak
        else
            echo "${var}=$(openssl rand -hex 24)" >> .env
        fi
    done
    echo "Auto-generated ${missing[*]}"
}

ensure_redis_passwords

# Issue #874: backend + scheduler run as UID 1000 (non-root). Ensure the host
# path bind-mounted at /data exists with the right owner BEFORE compose up,
# otherwise Docker creates it root-owned and UID 1000 cannot write trinity.db.
# Idempotent — re-running on a correctly-owned dir is a no-op. macOS Docker
# Desktop translates UIDs through osxfs / virtiofs so the chown is mostly
# cosmetic there; on Linux it is load-bearing.
ensure_data_path_ownership() {
    # Mirror the default used by docker-compose.prod.yml: ${TRINITY_DATA_PATH:-./trinity-data}.
    # Dev compose uses a named volume and is unaffected.
    local data_path
    data_path="${TRINITY_DATA_PATH:-}"
    [ -z "$data_path" ] && data_path=$(grep -E '^TRINITY_DATA_PATH=' .env 2>/dev/null | cut -d'=' -f2-)
    [ -z "$data_path" ] && data_path="./trinity-data"

    mkdir -p "$data_path"
    # Only chown on Linux. macOS would `chown 1000:1000` to a user that
    # doesn't exist (no fail, but pointless), and Docker Desktop ignores it.
    if [ "$(uname -s)" = "Linux" ]; then
        if [ "$(stat -c '%u' "$data_path" 2>/dev/null)" != "1000" ]; then
            if ! chown -R 1000:1000 "$data_path" 2>/dev/null; then
                sudo chown -R 1000:1000 "$data_path" || {
                    echo "ERROR: failed to chown $data_path to 1000:1000."
                    echo "       Backend will fail to create /data/trinity.db. Run manually:"
                    echo "         sudo chown -R 1000:1000 \"$data_path\""
                    exit 1
                }
            fi
        fi
    fi
}

ensure_data_path_ownership

# Issue #874 / #1131: backend runs as UID 1000 (non-root) but still needs to
# talk to /var/run/docker.sock, so compose joins it to the socket's group via
# `group_add: ["${DOCKER_GID:-999}"]`. The GID it must join is the group that
# owns the socket *as a container sees it*, which varies by runtime: a Linux
# bind mount exposes the host `docker` group (Debian/Ubuntu 999, RHEL/Fedora
# ~991, Arch 990); Docker Desktop / Colima / Rancher present it root-group-owned
# (GID 0). The old code wrongly assumed Docker Desktop *ignores* group_add and
# returned early on non-Linux, leaving the default 999 — Docker Desktop does
# NOT ignore it, so the backend was denied socket access and the Agents page
# silently showed "No agents" (#1131).
#
# So detect the value that is correct everywhere by probing the GID a throwaway
# container sees on the very socket compose mounts. The probe is the PRIMARY
# path on every runtime: a host-side `getent group docker` only sees the host
# docker group and silently mis-detects whenever the in-container socket GID
# differs from it — not just non-Linux, but Linux Docker Desktop / rootless /
# Colima too (all present the socket root-group-owned, GID 0, while a `docker`
# group may still exist on the host at some other GID). On a native Linux daemon
# the bind mount preserves the host docker-group GID, so the probe returns the
# same value `getent` would; `getent` survives only as the offline Linux
# fallback for when the probe can't run (daemon down, alpine unpullable, or
# SELinux denies the socket bind). An explicit DOCKER_GID=<n> in .env always
# wins (no probe). Note: by the time this runs the daemon must be up anyway —
# the base-image check and `compose up` below both require it — so the probe's
# daemon dependency costs nothing the rest of start.sh doesn't already need.
_probe_docker_gid() {
    # GID of /var/run/docker.sock as a container sees it — the value the backend
    # must join. Uses the same host socket path compose mounts, so it introduces
    # no new assumption. `stat -c` is supported by both alpine and busybox; tr
    # strips the trailing newline and any non-digit noise. The pipe makes the
    # function exit 0 even when the daemon is down, so `set -e` never trips here.
    docker run --rm -v /var/run/docker.sock:/var/run/docker.sock \
        alpine stat -c '%g' /var/run/docker.sock 2>/dev/null | tr -dc '0-9'
}
ensure_docker_gid() {
    # Respect an explicit override — any DOCKER_GID=<digits>, including 0.
    grep -qE '^DOCKER_GID=[0-9]+' .env 2>/dev/null && return 0
    # Probe FIRST — it reads the GID the backend container will actually see on
    # the socket compose mounts, so it is correct on every runtime (Docker
    # Desktop / Colima / rootless → 0; native Linux daemon → host docker GID).
    local detected
    detected=$(_probe_docker_gid)
    # Offline Linux fallback only when the probe couldn't run (daemon down,
    # alpine unpullable, SELinux denied the bind): on a Linux bind mount the
    # host docker-group GID is the best available guess. Non-Linux has no
    # offline fallback — warn below.
    if [ -z "$detected" ] && [ "$(uname -s)" = "Linux" ]; then
        detected=$(getent group docker 2>/dev/null | cut -d: -f3)
    fi
    if [ -z "$detected" ]; then
        echo "WARNING: could not determine docker.sock group GID (daemon down or probe"
        echo "         image unavailable). Set DOCKER_GID=<gid> in .env (Docker Desktop: 0)"
        echo "         and re-run. Compose falls back to 999 (Debian/Ubuntu default)."
        return 0
    fi
    if grep -qE '^DOCKER_GID=$' .env 2>/dev/null; then
        sed -i.bak "s/^DOCKER_GID=$/DOCKER_GID=${detected}/" .env && rm -f .env.bak
    else
        echo "DOCKER_GID=${detected}" >> .env
    fi
    echo "Set DOCKER_GID=${detected} (docker.sock in-container group)"
}

ensure_docker_gid

# --- Resolve the hosted image tag (#2280) ------------------------------------
# Precedence: an explicit shell/CI value > `.env` > `latest`. Reading `.env` is
# the whole point: it is where every other knob in this install lives and the
# only place an --unattended or marketplace install can persist a pin. Exporting
# an unconditional default instead — as this first shipped — silently beat the
# operator's own `.env` line, because compose gives the shell environment
# precedence over `.env`. The summary then printed `Currently pinned to: latest`
# as though they had chosen it, which is exactly the unscheduled upgrade the
# rest of this script warns about.
#
# Mirrors the FRONTEND_PORT read further down; `cut -d'=' -f2-` keeps a value
# containing '=' intact, and `tail -1` matches shell/compose last-wins semantics
# for a duplicated key.
resolve_image_tag() {
    if [ -z "${TRINITY_IMAGE_TAG:-}" ]; then
        TRINITY_IMAGE_TAG=$(grep -E '^TRINITY_IMAGE_TAG=' .env 2>/dev/null | tail -1 | cut -d'=' -f2- | tr -d '[:space:]')
    fi
    TRINITY_IMAGE_TAG="${TRINITY_IMAGE_TAG:-latest}"
    export TRINITY_IMAGE_TAG
}
resolve_image_tag

# --- Activate the Cloudflare Tunnel profile when a token is present (#2280) ---
# `cloudflared` is profile-gated (`profiles: ["tunnel"]`), so it starts only
# under `--profile tunnel`. docs/DEPLOYMENT.md presents the tunnel as the
# default posture for a public instance and says the service "is already in the
# compose file, set TUNNEL_TOKEN" — which was true and useless: setting the
# token and running this script produced no tunnel container, no error, and left
# the instance in the plain-HTTP-on-a-public-IPv4 state the same table says to
# avoid. A non-empty TUNNEL_TOKEN is unambiguous intent, so honour it.
#
# Hosted only: the dev stack is a localhost workflow and its file set is
# untouched by this change.
if [ "$HOSTED" = "1" ]; then
    _tunnel_token="${TUNNEL_TOKEN:-$(grep -E '^TUNNEL_TOKEN=' .env 2>/dev/null | tail -1 | cut -d'=' -f2- | tr -d '[:space:]')}"
    if [ -n "$_tunnel_token" ]; then
        COMPOSE_FILES+=(--profile tunnel)
        echo "TUNNEL_TOKEN set → starting the Cloudflare Tunnel (profile 'tunnel')."
    fi
fi

# --- Refuse a silent dev -> hosted data switch (#2280) ------------------------
# Dev and hosted share a compose project name (the directory basename) but NOT a
# /data source: docker-compose.yml mounts the named volume `trinity-data`, while
# hosted (inheriting prod) binds ${TRINITY_DATA_PATH:-./trinity-data}. So
# re-running this script with --hosted in a checkout that has been running the
# dev stack comes up against an EMPTY database and migrates from zero, while the
# real one sits untouched in the named volume — and Redis (`redis-data`, a named
# volume both files share) is NOT reset, so the result is a half-migrated
# install with live session/lock state pointing at rows that no longer exist.
#
# Nothing about "the same script, only the image source differs" prepares an
# operator for that, so refuse rather than warn: the failure is silent, and by
# the time it is noticed the fresh DB may have been written to.
if [ "$HOSTED" = "1" ]; then
    _data_path="${TRINITY_DATA_PATH:-$(grep -E '^TRINITY_DATA_PATH=' .env 2>/dev/null | tail -1 | cut -d'=' -f2- | tr -d '[:space:]')}"
    _data_path="${_data_path:-./trinity-data}"
    _project=$(basename "$(pwd)" | tr '[:upper:]' '[:lower:]' | tr -cd '[:alnum:]')
    if [ ! -f "${_data_path}/trinity.db" ] \
       && docker volume inspect "${_project}_trinity-data" >/dev/null 2>&1; then
        echo "❌ Refusing to start: this checkout has a dev-stack database that --hosted cannot see." >&2
        echo "" >&2
        echo "   Found: docker volume '${_project}_trinity-data' (written by docker-compose.yml)" >&2
        echo "   Wanted: ${_data_path}/trinity.db (the bind mount docker-compose.hosted.yml uses)" >&2
        echo "" >&2
        echo "   Starting anyway would migrate a NEW empty database from zero while the real" >&2
        echo "   one stays in the volume — and Redis is shared between the two stacks, so you" >&2
        echo "   would get a half-migrated install rather than a clean one." >&2
        echo "" >&2
        echo "   Copy the data across first (with the stack stopped):" >&2
        echo "       docker compose stop" >&2
        echo "       mkdir -p ${_data_path}" >&2
        echo "       docker run --rm -v ${_project}_trinity-data:/from -v \"\$(pwd)/${_data_path#./}\":/to \\" >&2
        echo "           alpine sh -c 'cp -a /from/. /to/'" >&2
        echo "       ./scripts/deploy/start.sh --hosted" >&2
        echo "" >&2
        echo "   Or, to deliberately start fresh, set TRINITY_DATA_PATH to a new directory." >&2
        exit 1
    fi
fi

# Check base image before starting — without it, agent creation will silently fail.
#
# #2280: the backend creates agent containers from the literal local tag
# `trinity-agent-base:latest` (hardcoded in services/agent_service/lifecycle.py,
# allowlisted as `trinity-agent-base:*` by SEC-172), and compose cannot retag —
# so hosted mode PULLS the GHCR copy and tags it locally rather than spending
# 5-10 minutes building it. Retagging keeps the SEC-172 allowlist and the
# #1809 image-drift check untouched: both read the container's own reference,
# which stays `trinity-agent-base:latest` either way.
if [ "$HOSTED" = "1" ]; then
    _base_remote="ghcr.io/abilityai/trinity-agent-base:${TRINITY_IMAGE_TAG}"
    echo "Pulling agent base image (${_base_remote})..."
    if ! docker pull "$_base_remote"; then
        echo ""
        echo "ERROR: could not pull ${_base_remote}."
        echo "       Hosted mode cannot fall back to a local build — that is the"
        echo "       5-10 minute first boot it exists to avoid. Check network and"
        echo "       image-tag spelling (TRINITY_IMAGE_TAG=${TRINITY_IMAGE_TAG}), or"
        echo "       drop --hosted to build from source instead."
        exit 1
    fi
    docker tag "$_base_remote" trinity-agent-base:latest
    echo "Tagged locally as trinity-agent-base:latest (the reference the backend creates agents from)."
    echo ""
elif ! docker images --format "{{.Repository}}:{{.Tag}}" | grep -q "trinity-agent-base:latest"; then
    echo "⚠️  trinity-agent-base:latest not found."
    echo "   Building base agent image first (required for agent creation)..."
    echo ""
    ./scripts/deploy/build-base-image.sh
    echo ""
fi

# Build-time provenance (#926). Export git commit/branch/build-date so
# docker-compose's `backend.build.args` block forwards them as Dockerfile
# ARGs → ENV vars → `GET /api/version` payload. Best-effort: if the host
# isn't a git checkout (CI tarball install) fall back to "unknown" so the
# downstream Dockerfile defaults still produce a well-typed response.
# #2280: skipped in hosted mode. These feed `backend.build.args`, and
# docker-compose.hosted.yml has no build block — the provenance a pulled image
# reports was stamped by the publish workflow at release time, and re-exporting
# the local checkout's git state here would be, at best, inert and, at worst, a
# claim about a build this host did not do.
if [ "$HOSTED" != "1" ] && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    export GIT_COMMIT=$(git rev-parse HEAD)
    export GIT_COMMIT_SUBJECT=$(git log -1 --pretty=%s)
    export GIT_COMMIT_TIMESTAMP=$(git log -1 --pretty=%cI)
    export GIT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
    # #993: dynamic version = curated semver (VERSION file) + git short sha
    # (+ ".dirty" when the tree has uncommitted changes), e.g.
    # "0.9.0+g4c640b6e". Env-stamped so dev and prod agree per commit.
    _base_ver=$(cat VERSION 2>/dev/null || echo unknown)
    _short_sha=$(git rev-parse --short=8 HEAD)
    git diff --quiet HEAD 2>/dev/null || _short_sha="${_short_sha}.dirty"
    export VERSION="${_base_ver}+g${_short_sha}"
fi
export BUILD_DATE=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# --- Docker Desktop Vector log-source fix (#1432) -----------------------------
# On Docker Desktop / VM-based Docker runtimes, Vector's default `docker_logs`
# source busy-loops and pegs the Docker VM at ~4 cores. Swap it for an on-disk
# file source via docker-compose.override.yml (auto-merged by `docker compose up`).
# Native Linux dockerd is unaffected. Opt out: TRINITY_LOCAL_LOG_SOURCE=docker.
# Force on: TRINITY_LOCAL_LOG_SOURCE=file. Default: auto-detect Docker Desktop.
# Hosted passes an explicit -f, which disables override AUTO-merge — but the
# override is still applicable, so it is appended to COMPOSE_FILES explicitly
# below rather than switched off. Forcing `_log_src=docker` under --hosted (as
# this first shipped) meant a hosted install on Docker Desktop or any VM-backed
# runtime shipped the very `docker_logs` source #1432 exists to avoid, and
# Vector busy-loops and pegs the Docker VM. The vector service is byte-identical
# between docker-compose.yml and the hosted file, so the override merges cleanly
# onto either.
_log_src="${TRINITY_LOCAL_LOG_SOURCE:-auto}"
if [ "$_log_src" = "auto" ]; then
    if docker info 2>/dev/null | grep -qi 'Docker Desktop'; then
        _log_src=file
    else
        _log_src=docker
    fi
fi
if [ "$_log_src" = "file" ]; then
    if [ ! -f docker-compose.override.yml ]; then
        cp docker-compose.override.example.yml docker-compose.override.yml
        echo "Docker Desktop detected → using the on-disk Vector log source (#1432)."
        echo "  Created docker-compose.override.yml. To opt out: delete it, or set"
        echo "  TRINITY_LOCAL_LOG_SOURCE=docker. Local logs land in /data/logs/local-*.json;"
        echo "  prefer 'docker compose logs -f <service>' to tail a single service."
    fi
    # Dev gets this by auto-merge; hosted must ask for it by name, because its
    # explicit -f turns auto-merge off. Order matters — the override has to come
    # after the base file it patches.
    if [ "$HOSTED" = "1" ]; then
        COMPOSE_FILES+=(-f docker-compose.override.yml)
    fi
fi
# -----------------------------------------------------------------------------

if [ "$HOSTED" = "1" ]; then
    echo "Pulling platform images (tag: ${TRINITY_IMAGE_TAG})..."
    docker compose "${COMPOSE_FILES[@]}" pull
    echo ""
fi

echo "Starting services..."
docker compose "${COMPOSE_FILES[@]}" up -d

# Read FRONTEND_PORT from .env or use default (needed for the serving check + URL).
FRONTEND_PORT=${FRONTEND_PORT:-$(grep -E '^FRONTEND_PORT=' .env 2>/dev/null | cut -d'=' -f2 || echo "80")}
FRONTEND_PORT=${FRONTEND_PORT:-80}

# Verify the stack is actually SERVING, not just "containers started" (#39).
# Poll the backend health endpoint (authoritative — it's up only after DB
# migrations + lifespan init) and the Web UI. Bounded so a wedged boot doesn't
# hang an agent-run install forever; a timeout downgrades to a warning, not a
# hard failure, because the containers may still be finishing (image pulls, etc).
echo ""
echo "Waiting for the stack to come up (migrations + health)..."
SERVING_OK=0
_deadline=$(( $(date +%s) + 180 ))
while [ "$(date +%s)" -lt "$_deadline" ]; do
    if curl -fsS -m 3 "http://localhost:8000/health" >/dev/null 2>&1; then
        SERVING_OK=1
        break
    fi
    sleep 3
    printf '.'
done
echo ""

echo ""
echo "====================================="
if [ "$SERVING_OK" = "1" ]; then
    echo "Trinity Agent Platform - Ready! ✅"
else
    echo "Trinity Agent Platform - Started (not yet serving) ⚠️"
fi
echo "====================================="
echo ""
if [ "$SERVING_OK" != "1" ]; then
    echo "⚠️  The backend health check at http://localhost:8000/health did not"
    echo "    respond within 180s. Containers are up but may still be initializing"
    if [ "$HOSTED" = "1" ]; then
        echo "    (image pulls / migrations). Check:  docker compose -f docker-compose.hosted.yml logs -f backend"
    else
        echo "    (first-run image build / migrations). Check:  docker compose logs -f backend"
    fi
    echo "    Re-running start.sh is safe once it settles."
    echo ""
fi

# Web UI URL (used in the summary below).
if [ "$FRONTEND_PORT" = "80" ]; then
    WEB_UI_URL="http://localhost"
else
    WEB_UI_URL="http://localhost:$FRONTEND_PORT"
fi

echo "Access points:"
echo "  - Web UI:       ${WEB_UI_URL}"
echo "  - Backend API:  http://localhost:8000/docs"
echo "  - MCP Server:   http://localhost:8080/mcp"
echo ""

# --- Next steps card (#39) ----------------------------------------------------
echo "── Your next steps ──────────────────────────────────────────────────────"
echo ""
echo "  1. Open the Web UI:  ${WEB_UI_URL}"
if [ -n "$GENERATED_ADMIN_PASSWORD" ]; then
    echo "     Log in as 'admin' with this AUTO-GENERATED password (save it now —"
    echo "     it is stored in .env as ADMIN_PASSWORD and won't be shown again):"
    echo ""
    echo "         admin / ${GENERATED_ADMIN_PASSWORD}"
    echo ""
else
    # #2381: this used to say "then complete the first-run setup wizard". The
    # wizard only appears on an install with no admin account; setting
    # ADMIN_PASSWORD (which this script requires) provisions one at boot, so
    # there is no wizard to complete. Binding a sign-in email is now a normal
    # post-login action in Settings, not a first-run gate.
    echo "     Log in as 'admin' with the ADMIN_PASSWORD from your .env."
    echo "     Then bind a sign-in email in Settings → General (optional, but it"
    echo "     lets you sign in by email instead of the fixed 'admin' username)."
fi
echo "  2. Install the agent-dev plugins (in Claude Code):"
echo "         /plugin marketplace add abilityai/abilities"
echo "         /plugin install trinity@abilityai"
echo "     then run  /trinity:onboard  to build & connect your first agent."
echo "  3. Or create an agent straight from the UI: Create Agent → pick a template."
if [ "$MODEL_KEY_MISSING" = "1" ]; then
    echo ""
    echo "  ⚠️  No model API key detected in .env — agents can't run until you set one."
    echo "     Add ANTHROPIC_API_KEY=... (or GOOGLE_API_KEY / a Claude subscription"
    echo "     token) to .env and re-run start.sh, or set it in Settings after login."
fi
echo ""
echo "─────────────────────────────────────────────────────────────────────────"
echo ""
# #2280: hosted installs have no build context, so the stale-image remedy is a
# pull, not a build — and every compose command needs the explicit -f, since
# hosted deliberately opts out of the default file merge. Printing the dev
# commands to a hosted operator sends them at a `build` that cannot work.
if [ "$HOSTED" = "1" ]; then
    _cf="-f docker-compose.hosted.yml"
    echo "To view logs:      docker compose ${_cf} logs -f"
    echo "To stop services:  docker compose ${_cf} stop"
    echo "  (use 'stop', NOT 'down' — 'down' destroys agent containers)"
    echo ""
    echo "To upgrade: set TRINITY_IMAGE_TAG to the release you want, then re-run"
    echo "this script with --hosted. It re-pulls the platform images AND the agent"
    echo "base image; a plain 'docker compose ${_cf} pull' skips the base image,"
    echo "which is not a compose service, and leaves agents on the old runtime."
    echo "Currently pinned to: TRINITY_IMAGE_TAG=${TRINITY_IMAGE_TAG}"
    if [ "$TRINITY_IMAGE_TAG" = "latest" ]; then
        echo "  ⚠️  'latest' moves on every Trinity release. Pin a version on any"
        echo "     install you intend to keep, or your next run is an unscheduled"
        echo "     upgrade:"
        echo "         echo 'TRINITY_IMAGE_TAG=v0.9.0' >> .env"
        echo "     (.env, not the shell — that is where the pin survives a reboot"
        echo "      and whoever runs the upgrade next.)"
    fi
    echo ""
else
    echo "To view logs:      docker compose logs -f"
    echo "To stop services:  docker compose stop"
    echo "  (use 'stop', NOT 'down' — 'down' destroys agent containers)"
    echo ""
    echo "Just pulled new code? If services fail with ModuleNotFoundError or"
    echo "the UI shows 'Disconnected', the platform images may be stale —"
    echo "rebuild with:  docker compose build && docker compose up -d"
    echo "(See docs/DEPLOYMENT.md → Troubleshooting → Stale platform images.)"
    echo ""
fi

