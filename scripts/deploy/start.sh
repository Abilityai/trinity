#!/bin/bash

set -e

cd "$(dirname "$0")/../.."

echo "====================================="
echo "Trinity Agent Platform - Starting"
echo "====================================="
echo ""

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

# ADMIN_PASSWORD has no sensible default — operator must choose. Fail fast
# rather than booting into a state the operator can't log into. (#443)
if ! grep -qE '^ADMIN_PASSWORD=.+' .env 2>/dev/null; then
    cat >&2 <<EOF

ERROR: ADMIN_PASSWORD is blank in .env.
       Choose a strong password (12+ chars; the backend will reject
       weak defaults like "password" or "admin"), then re-run start.sh.

EOF
    exit 1
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

# Check base image before starting — without it, agent creation will silently fail
if ! docker images --format "{{.Repository}}:{{.Tag}}" | grep -q "trinity-agent-base:latest"; then
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
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
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
fi
# -----------------------------------------------------------------------------

echo "Starting services..."
docker compose up -d

echo ""
echo "Waiting for services to be ready..."
sleep 5

echo ""
echo "====================================="
echo "Trinity Agent Platform - Ready!"
echo "====================================="
echo ""
# Read FRONTEND_PORT from .env or use default
FRONTEND_PORT=${FRONTEND_PORT:-$(grep -E '^FRONTEND_PORT=' .env 2>/dev/null | cut -d'=' -f2 || echo "80")}
FRONTEND_PORT=${FRONTEND_PORT:-80}

echo "Access points:"
if [ "$FRONTEND_PORT" = "80" ]; then
    echo "  - Web UI:       http://localhost (login: admin / ADMIN_PASSWORD from .env)"
else
    echo "  - Web UI:       http://localhost:$FRONTEND_PORT (login: admin / ADMIN_PASSWORD from .env)"
fi
echo "  - Backend API:  http://localhost:8000/docs"
echo "  - MCP Server:   http://localhost:8080/mcp"
echo ""
echo "To view logs:"
echo "  docker compose logs -f"
echo ""
echo "To stop services:"
echo "  docker compose stop"
echo ""
echo "NOTE: Use 'stop' not 'down' — 'down' destroys agent containers."
echo ""
echo "Just pulled new code? If services fail with ModuleNotFoundError or"
echo "the UI shows 'Disconnected', the platform images may be stale —"
echo "rebuild with:  docker compose build && docker compose up -d"
echo "(See docs/DEPLOYMENT.md → Troubleshooting → Stale platform images.)"
echo ""

