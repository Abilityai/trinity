#!/bin/bash
# Trinity Quick Start
# One-command setup for a new Trinity instance.
#
# Usage:
#   ./quickstart.sh            — interactive guided setup
#   ./quickstart.sh --defaults — non-interactive with auto-generated secrets

set -e

cd "$(dirname "$0")"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BOLD='\033[1m'
NC='\033[0m'

DEFAULTS_MODE=false
TAILNET_MODE=false
TAILNET_HOSTNAME=""
while [ $# -gt 0 ]; do
    case "$1" in
        --defaults)
            DEFAULTS_MODE=true
            ;;
        --tailnet)
            TAILNET_MODE=true
            ;;
        --tailnet-hostname)
            TAILNET_MODE=true
            TAILNET_HOSTNAME="${2:-}"
            shift
            ;;
    esac
    shift
done

if [ "$TAILNET_MODE" = true ]; then
    # shellcheck source=scripts/lib/tailnet.sh
    source "$(dirname "$0")/scripts/lib/tailnet.sh"
fi

echo ""
echo -e "${BOLD}=================================================${NC}"
echo -e "${BOLD}  Trinity Agent Platform — Quick Start${NC}"
echo -e "${BOLD}=================================================${NC}"
echo ""

# ── 1. Prerequisites ──────────────────────────────────────────────────────────

echo -e "${BOLD}Checking prerequisites...${NC}"

if ! command -v docker &>/dev/null; then
    echo -e "${RED}✗ Docker not found. Install Docker Desktop: https://docs.docker.com/get-docker/${NC}"
    exit 1
fi

if ! docker info &>/dev/null; then
    echo -e "${RED}✗ Docker is not running. Start Docker Desktop and try again.${NC}"
    exit 1
fi
echo -e "${GREEN}✓ Docker is running${NC}"

if ! docker compose version &>/dev/null; then
    echo -e "${RED}✗ Docker Compose v2 not found. Update Docker Desktop or install the compose plugin.${NC}"
    exit 1
fi
echo -e "${GREEN}✓ Docker Compose v2 available${NC}"
echo ""

# ── 2. Environment ────────────────────────────────────────────────────────────

echo -e "${BOLD}Configuring environment...${NC}"

if [ ! -f .env ]; then
    cp .env.example .env
    echo "  Created .env from template"
fi

# Auto-generate missing secrets
for var in SECRET_KEY INTERNAL_API_SECRET; do
    val=$(grep -E "^${var}=" .env | cut -d'=' -f2-)
    if [ -z "$val" ]; then
        new_val=$(openssl rand -hex 32)
        sed -i.bak "s|^${var}=.*|${var}=${new_val}|" .env && rm -f .env.bak
        echo "  Auto-generated $var"
    fi
done

# Auto-generate CREDENTIAL_ENCRYPTION_KEY if blank
val=$(grep -E '^CREDENTIAL_ENCRYPTION_KEY=' .env | cut -d'=' -f2-)
if [ -z "$val" ]; then
    new_val=$(openssl rand -hex 32)
    sed -i.bak "s|^CREDENTIAL_ENCRYPTION_KEY=.*|CREDENTIAL_ENCRYPTION_KEY=${new_val}|" .env && rm -f .env.bak
    echo "  Auto-generated CREDENTIAL_ENCRYPTION_KEY (do not change after first boot)"
fi

# ADMIN_PASSWORD
admin_pass=$(grep -E '^ADMIN_PASSWORD=' .env | cut -d'=' -f2-)
if [ -z "$admin_pass" ]; then
    if [ "$DEFAULTS_MODE" = true ]; then
        admin_pass=$(openssl rand -base64 16 | tr -d '/+=' | head -c 16)
        sed -i.bak "s|^ADMIN_PASSWORD=.*|ADMIN_PASSWORD=${admin_pass}|" .env && rm -f .env.bak
        echo "  Auto-generated ADMIN_PASSWORD: ${BOLD}${admin_pass}${NC}"
        echo -e "  ${YELLOW}Save this password — it will not be shown again.${NC}"
    else
        echo ""
        echo -e "  ${YELLOW}Set an admin password (minimum 12 characters):${NC}"
        while true; do
            read -rsp "  ADMIN_PASSWORD: " admin_pass
            echo ""
            if [ ${#admin_pass} -ge 12 ]; then
                break
            fi
            echo -e "  ${RED}Too short — use at least 12 characters.${NC}"
        done
        sed -i.bak "s|^ADMIN_PASSWORD=.*|ADMIN_PASSWORD=${admin_pass}|" .env && rm -f .env.bak
        echo "  ✓ Admin password saved to .env"
    fi
fi

# ANTHROPIC_API_KEY
anthropic_key=$(grep -E '^ANTHROPIC_API_KEY=' .env | cut -d'=' -f2-)
if [ -z "$anthropic_key" ] && [ "$DEFAULTS_MODE" = false ]; then
    echo ""
    echo "  Anthropic API key (required for agents to use Claude)."
    echo "  Get one at: https://console.anthropic.com/api-keys"
    echo "  Leave blank to configure later in Settings:"
    read -rsp "  ANTHROPIC_API_KEY (or Enter to skip): " anthropic_key
    echo ""
    if [ -n "$anthropic_key" ]; then
        sed -i.bak "s|^ANTHROPIC_API_KEY=.*|ANTHROPIC_API_KEY=${anthropic_key}|" .env && rm -f .env.bak
        echo "  ✓ Anthropic API key saved"
    else
        echo "  Skipped — configure later in Settings → Anthropic API Key"
    fi
fi

echo ""
echo -e "${GREEN}✓ Environment configured${NC}"
echo ""

if [ "$TAILNET_MODE" = true ]; then
    echo -e "${BOLD}Configuring tailnet deployment...${NC}"
    tn_check_tailscale || true
    if [ -n "$TAILNET_HOSTNAME" ]; then
        _tn_hostname=$(tn_detect_hostname --tailnet-hostname "$TAILNET_HOSTNAME")
    else
        _tn_hostname=$(tn_detect_hostname) || _tn_hostname=""
    fi
    tn_check_ports
    # Intentionally generate the default override; no script args are forwarded here.
    # shellcheck disable=SC2119
    tn_generate_override
    [ -n "$_tn_hostname" ] && tn_apply_env "$_tn_hostname"
    echo -e "${GREEN}✓ Tailnet deployment files configured${NC}"
    echo ""
fi

# ── 3. Base image ─────────────────────────────────────────────────────────────

echo -e "${BOLD}Checking base agent image...${NC}"
if docker images --format "{{.Repository}}:{{.Tag}}" | grep -q "trinity-agent-base:latest"; then
    echo -e "${GREEN}✓ trinity-agent-base:latest already exists${NC}"
else
    echo "  Building trinity-agent-base:latest (5-10 minutes on first run)..."
    ./scripts/deploy/build-base-image.sh
    echo -e "${GREEN}✓ Base image built${NC}"
fi
echo ""

# ── 4. Start services ─────────────────────────────────────────────────────────

echo -e "${BOLD}Starting Trinity services...${NC}"
docker compose up -d

if [ "$TAILNET_MODE" = true ] && [ -n "${_tn_hostname:-}" ]; then
    tn_configure_serve || true
    tn_print_summary "$_tn_hostname"
fi

echo ""
echo "Waiting for services to initialize..."
sleep 8
echo ""

# ── 5. Verify ─────────────────────────────────────────────────────────────────

echo -e "${BOLD}Verifying platform health...${NC}"
./scripts/deploy/verify-platform.sh || true
echo ""

# ── 6. Summary ────────────────────────────────────────────────────────────────

FRONTEND_PORT=$(grep -E '^FRONTEND_PORT=' .env 2>/dev/null | cut -d'=' -f2 || echo "")
FRONTEND_PORT=${FRONTEND_PORT:-80}
WEB_URL="http://localhost"
[ "$FRONTEND_PORT" != "80" ] && WEB_URL="http://localhost:${FRONTEND_PORT}"
if [ "$TAILNET_MODE" = true ] && [ -n "${_tn_hostname:-}" ]; then
    WEB_URL="https://${_tn_hostname}"
fi

echo ""
echo -e "${BOLD}=================================================${NC}"
echo -e "${GREEN}${BOLD}  Trinity is ready!${NC}"
echo -e "${BOLD}=================================================${NC}"
echo ""
echo "  Web UI:       $WEB_URL"
echo "  Backend API:  http://localhost:8000/docs"
echo "  MCP Server:   http://localhost:8080/mcp"
echo ""
echo "  Login:   admin / [your ADMIN_PASSWORD]"
echo ""
echo "Next steps:"
echo "  1. Open $WEB_URL and log in"
echo "  2. Go to Settings → Platform API Keys and create an MCP key"
echo "  3. In Claude Code: /trinity:connect"
echo "  4. Create your first agent: /trinity:onboard"
echo ""
echo "To stop:   docker compose stop"
echo "To check:  ./scripts/deploy/verify-platform.sh"
echo ""
