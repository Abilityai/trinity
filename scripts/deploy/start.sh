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

# Issue #874: backend joins the host's `docker` group via compose's group_add
# so UID 1000 can talk to /var/run/docker.sock. The group's GID varies by
# distro (Debian/Ubuntu: 999, RHEL/Fedora: ~991, Arch: 990). Auto-detect on
# Linux so RHEL hosts don't silently fail Docker SDK calls. macOS Docker
# Desktop ignores group_add — fall through with the default.
ensure_docker_gid() {
    if [ "$(uname -s)" != "Linux" ]; then
        return 0
    fi
    if grep -qE '^DOCKER_GID=[0-9]+' .env 2>/dev/null; then
        return 0
    fi
    local detected
    detected=$(getent group docker 2>/dev/null | cut -d: -f3)
    if [ -z "$detected" ]; then
        echo "WARNING: no 'docker' group on host. Backend may fail to reach docker.sock."
        echo "         Add a DOCKER_GID=<gid> override to .env if your daemon socket is"
        echo "         group-owned by a different group."
        return 0
    fi
    if grep -qE '^DOCKER_GID=$' .env 2>/dev/null; then
        sed -i.bak "s/^DOCKER_GID=$/DOCKER_GID=${detected}/" .env && rm -f .env.bak
    else
        echo "DOCKER_GID=${detected}" >> .env
    fi
    echo "Auto-detected DOCKER_GID=${detected} (Linux docker group)"
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

