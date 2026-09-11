#!/bin/bash

# Trinity Platform - Verification Script
# Checks that all core services are running and healthy

cd "$(dirname "$0")/../.."

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo "====================================="
echo "Trinity Platform - Health Check"
echo "====================================="
echo ""

all_running=true

# 1. Docker
echo "1. Checking Docker..."
if ! docker ps &> /dev/null; then
    echo -e "${RED}✗ Docker is not running${NC}"
    exit 1
fi
echo -e "${GREEN}✓ Docker is running${NC}"
echo ""

# 2. Core services
echo "2. Checking core services..."
services=("trinity-backend" "trinity-redis" "trinity-frontend" "trinity-mcp-server" "trinity-scheduler" "trinity-vector")
for service in "${services[@]}"; do
    if docker ps --filter "name=^/${service}$" --format "{{.Names}}" | grep -q "^${service}$"; then
        status=$(docker ps --filter "name=^/${service}$" --format "{{.Status}}")
        echo -e "${GREEN}✓ $service${NC} - $status"
    else
        echo -e "${RED}✗ $service is not running${NC}"
        all_running=false
    fi
done
echo ""

# 3. Health endpoints
echo "3. Checking health endpoints..."

# Backend
if curl -sf http://localhost:8000/health | grep -q "healthy\|ok\|status"; then
    echo -e "${GREEN}✓ Backend health (localhost:8000)${NC}"
else
    echo -e "${RED}✗ Backend health check failed${NC}"
    all_running=false
fi

# Scheduler (uses port 8001)
if curl -sf http://localhost:8001/health | grep -q "healthy\|ok\|status"; then
    echo -e "${GREEN}✓ Scheduler health (localhost:8001)${NC}"
else
    echo -e "${YELLOW}⚠ Scheduler health check failed (may still be starting)${NC}"
fi

# Frontend (port 80 or FRONTEND_PORT)
FRONTEND_PORT=${FRONTEND_PORT:-$(grep -E '^FRONTEND_PORT=' .env 2>/dev/null | cut -d'=' -f2 || echo "80")}
FRONTEND_PORT=${FRONTEND_PORT:-80}
FRONTEND_URL="http://localhost"
[ "$FRONTEND_PORT" != "80" ] && FRONTEND_URL="http://localhost:${FRONTEND_PORT}"

if curl -sf "$FRONTEND_URL" | grep -q -i "trinity\|html\|app"; then
    echo -e "${GREEN}✓ Frontend accessible ($FRONTEND_URL)${NC}"
else
    echo -e "${RED}✗ Frontend not accessible ($FRONTEND_URL)${NC}"
    all_running=false
fi

# MCP Server
if curl -sf http://localhost:8080/health | grep -q "healthy\|ok\|status"; then
    echo -e "${GREEN}✓ MCP Server health (localhost:8080)${NC}"
else
    echo -e "${YELLOW}⚠ MCP Server health check failed${NC}"
fi

# Vector
if curl -sf http://localhost:8686/health | grep -q "ok\|healthy"; then
    echo -e "${GREEN}✓ Vector log aggregator (localhost:8686)${NC}"
else
    echo -e "${YELLOW}⚠ Vector health check failed${NC}"
fi
echo ""

# 4. Base agent image
echo "4. Checking base agent image..."
if docker images --format "{{.Repository}}:{{.Tag}}" | grep -q "trinity-agent-base:latest"; then
    echo -e "${GREEN}✓ trinity-agent-base:latest exists${NC}"
else
    echo -e "${YELLOW}⚠ trinity-agent-base image not found${NC}"
    echo "  Agents cannot be created until you build it:"
    echo "  ./scripts/deploy/build-base-image.sh"
fi
echo ""

# 5. Configuration
echo "5. Checking configuration..."
if [ -f .env ]; then
    echo -e "${GREEN}✓ .env file exists${NC}"
    # Check critical vars are set
    # ent#580: a marketplace droplet leaves ADMIN_PASSWORD blank on purpose —
    # its admin is created in the browser, so blank is not a misconfiguration.
    admin_in_browser=0
    grep -qE '^ADMIN_PASSWORD_SOURCE=browser$' .env 2>/dev/null && admin_in_browser=1
    for var in SECRET_KEY CREDENTIAL_ENCRYPTION_KEY ADMIN_PASSWORD; do
        [ "$var" = "ADMIN_PASSWORD" ] && [ "$admin_in_browser" = 1 ] && continue
        val=$(grep -E "^${var}=" .env 2>/dev/null | cut -d'=' -f2-)
        if [ -z "$val" ]; then
            echo -e "${YELLOW}⚠ $var is not set in .env${NC}"
        fi
    done
else
    echo -e "${YELLOW}⚠ .env file not found — run: cp .env.example .env${NC}"
fi
echo ""

# Summary
echo "====================================="
if [ "$all_running" = true ]; then
    echo -e "${GREEN}✓ Platform is healthy!${NC}"
    echo ""
    echo "Access points:"
    echo "  - Web UI:       $FRONTEND_URL"
    echo "  - Backend API:  http://localhost:8000/docs"
    echo "  - MCP Server:   http://localhost:8080/mcp"
    echo "  - Scheduler:    http://localhost:8001/health"
    echo "  - Vector logs:  http://localhost:8686/health"
    echo ""
    if [ "${admin_in_browser:-0}" = 1 ] && ! grep -qE '^ADMIN_PASSWORD=.+' .env 2>/dev/null; then
        echo "Login: the admin account created in the browser (none yet? open the Web UI and create it)"
    else
        echo "Login: admin / [your ADMIN_PASSWORD from .env]"
    fi
else
    echo -e "${RED}✗ Some services are not running${NC}"
    echo ""
    echo "Try:"
    echo "  docker compose logs -f"
    echo "  docker compose restart"
    exit 1
fi
echo "====================================="
