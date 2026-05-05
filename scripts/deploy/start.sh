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

# Auto-generate CREDENTIAL_ENCRYPTION_KEY if not set
if grep -qE '^CREDENTIAL_ENCRYPTION_KEY=$' .env 2>/dev/null || ! grep -q 'CREDENTIAL_ENCRYPTION_KEY' .env 2>/dev/null; then
    NEW_KEY=$(openssl rand -hex 32)
    if grep -q 'CREDENTIAL_ENCRYPTION_KEY' .env; then
        sed -i.bak "s/^CREDENTIAL_ENCRYPTION_KEY=$/CREDENTIAL_ENCRYPTION_KEY=${NEW_KEY}/" .env && rm -f .env.bak
    else
        echo "CREDENTIAL_ENCRYPTION_KEY=${NEW_KEY}" >> .env
    fi
    echo "Auto-generated CREDENTIAL_ENCRYPTION_KEY"
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

# Check base image before starting — without it, agent creation will silently fail
if ! docker images --format "{{.Repository}}:{{.Tag}}" | grep -q "trinity-agent-base:latest"; then
    echo "⚠️  trinity-agent-base:latest not found."
    echo "   Building base agent image first (required for agent creation)..."
    echo ""
    ./scripts/deploy/build-base-image.sh
    echo ""
fi

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
    echo "  - Web UI:       http://localhost (login: admin/password)"
else
    echo "  - Web UI:       http://localhost:$FRONTEND_PORT (login: admin/password)"
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

