#!/bin/bash
# GCP Deployment Script for Trinity Agent Platform
#
# Prerequisites:
#   1. Copy deploy.config.example to deploy.config
#   2. Fill in your GCP project, zone, instance, and domain
#   3. Run: ./scripts/deploy/gcp-deploy.sh
#
# Usage:
#   ./scripts/deploy/gcp-deploy.sh          # Full deployment
#   ./scripts/deploy/gcp-deploy.sh sync     # Sync files only
#   ./scripts/deploy/gcp-deploy.sh restart  # Restart services only
#   ./scripts/deploy/gcp-deploy.sh status   # Check status

set -e

# Script directory
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
CONFIG_FILE="${PROJECT_ROOT}/deploy.config"

# Check for config file
if [ ! -f "${CONFIG_FILE}" ]; then
    echo "Error: deploy.config not found!"
    echo ""
    echo "To set up deployment:"
    echo "  1. cp deploy.config.example deploy.config"
    echo "  2. Edit deploy.config with your GCP settings"
    echo "  3. Run this script again"
    exit 1
fi

# Load configuration
source "${CONFIG_FILE}"

# Validate required variables
if [ -z "${GCP_PROJECT}" ] || [ "${GCP_PROJECT}" = "your-gcp-project-id" ]; then
    echo "Error: GCP_PROJECT not configured in deploy.config"
    exit 1
fi

if [ -z "${GCP_INSTANCE}" ] || [ "${GCP_INSTANCE}" = "your-vm-name" ]; then
    echo "Error: GCP_INSTANCE not configured in deploy.config"
    exit 1
fi

if [ -z "${GCP_ZONE}" ]; then
    echo "Error: GCP_ZONE not configured in deploy.config"
    exit 1
fi

echo "====================================="
echo "Trinity Platform - GCP Deployment"
echo "====================================="
echo ""
echo "Project:  ${GCP_PROJECT}"
echo "Instance: ${GCP_INSTANCE}"
echo "Zone:     ${GCP_ZONE}"
echo "Domain:   ${DOMAIN:-not set}"
echo ""

# Check if gcloud is authenticated
if ! gcloud auth list --filter=status:ACTIVE --format="value(account)" 2>/dev/null | head -n1 > /dev/null; then
    echo "Error: Not authenticated with gcloud. Run 'gcloud auth login' first."
    exit 1
fi

# Set project
gcloud config set project "${GCP_PROJECT}" 2>/dev/null

# Function to sync files (using rsync to exclude node_modules, __pycache__, etc.)
sync_files() {
    echo "Syncing files to ${GCP_INSTANCE}..."

    # Create remote directories
    echo "  Creating remote directories..."
    gcloud compute ssh "${GCP_INSTANCE}" \
        --zone="${GCP_ZONE}" \
        --command="mkdir -p ${REMOTE_DIR} && mkdir -p ~/trinity-data && chmod 777 ~/trinity-data"

    # Create a temporary directory with files to upload
    echo "  Preparing files (excluding node_modules, __pycache__, .git)..."
    TEMP_DIR=$(mktemp -d)
    trap "rm -rf ${TEMP_DIR}" EXIT

    # Copy necessary files to temp directory
    cp "${PROJECT_ROOT}/docker-compose.prod.yml" "${TEMP_DIR}/"
    cp -r "${PROJECT_ROOT}/docker" "${TEMP_DIR}/"

    mkdir -p "${TEMP_DIR}/src"

    # Backend: copy .py files and module directories (no venv, __pycache__)
    mkdir -p "${TEMP_DIR}/src/backend"
    cp "${PROJECT_ROOT}/src/backend/"*.py "${TEMP_DIR}/src/backend/" 2>/dev/null || true
    # Copy routers, services, utils, db modules
    for module in routers services adapters utils db; do
        if [ -d "${PROJECT_ROOT}/src/backend/${module}" ]; then
            rsync -a --exclude '__pycache__' "${PROJECT_ROOT}/src/backend/${module}" "${TEMP_DIR}/src/backend/"
        fi
    done

    # Audit logger
    if [ -d "${PROJECT_ROOT}/src/audit-logger" ]; then
        rsync -a --exclude '__pycache__' "${PROJECT_ROOT}/src/audit-logger" "${TEMP_DIR}/src/"
    fi

    # Frontend: copy without node_modules (will npm install on server)
    mkdir -p "${TEMP_DIR}/src/frontend"
    rsync -a --exclude 'node_modules' --exclude '.git' --exclude 'dist' "${PROJECT_ROOT}/src/frontend/" "${TEMP_DIR}/src/frontend/"

    # MCP Server: copy without node_modules
    mkdir -p "${TEMP_DIR}/src/mcp-server"
    rsync -a --exclude 'node_modules' --exclude '.git' --exclude 'dist' "${PROJECT_ROOT}/src/mcp-server/" "${TEMP_DIR}/src/mcp-server/"

    # Config directory
    cp -r "${PROJECT_ROOT}/config" "${TEMP_DIR}/"

    # Upload
    echo "  Uploading to ${GCP_INSTANCE}..."
    gcloud compute scp --recurse \
        --zone="${GCP_ZONE}" \
        --compress \
        "${TEMP_DIR}/"* "${GCP_INSTANCE}:${REMOTE_DIR}/"

    echo "Files synced."
}

# Function to create .env file on remote
create_remote_env() {
    echo "Creating .env file on remote..."

    # Issue #692: refuse to bake the well-known "changeme" password into a prod
    # .env. Set ADMIN_PASSWORD in deploy.config (or in the shell) before running.
    if [ -z "${ADMIN_PASSWORD:-}" ] || [ "${ADMIN_PASSWORD}" = "changeme" ]; then
        echo "ERROR: ADMIN_PASSWORD must be set to a strong value in deploy.config (current: '${ADMIN_PASSWORD:-<unset>}'). Refusing to deploy with a well-known default." >&2
        return 1
    fi

    # Build env file content
    ENV_CONTENT="# Trinity Production Environment
SECRET_KEY=${SECRET_KEY:-$(openssl rand -hex 32)}
ADMIN_PASSWORD=${ADMIN_PASSWORD}
BACKEND_URL=${BACKEND_URL:-https://${DOMAIN}/api}
VITE_API_URL=${BACKEND_URL:-https://${DOMAIN}/api}

# Email Authentication
EMAIL_PROVIDER=${EMAIL_PROVIDER:-resend}
RESEND_API_KEY=${RESEND_API_KEY:-}
SMTP_FROM=${SMTP_FROM:-noreply@${DOMAIN}}

# GitHub PAT for template cloning
GITHUB_PAT=${GITHUB_PAT:-}

# OAuth (optional)
GOOGLE_CLIENT_ID=${GOOGLE_CLIENT_ID:-}
GOOGLE_CLIENT_SECRET=${GOOGLE_CLIENT_SECRET:-}
SLACK_CLIENT_ID=${SLACK_CLIENT_ID:-}
SLACK_CLIENT_SECRET=${SLACK_CLIENT_SECRET:-}
GITHUB_CLIENT_ID=${GITHUB_CLIENT_ID:-}
GITHUB_CLIENT_SECRET=${GITHUB_CLIENT_SECRET:-}
NOTION_CLIENT_ID=${NOTION_CLIENT_ID:-}
NOTION_CLIENT_SECRET=${NOTION_CLIENT_SECRET:-}

# Anthropic
ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY:-}

# Ports
BACKEND_PORT=${BACKEND_PORT:-8000}
FRONTEND_PORT=${FRONTEND_PORT:-80}

# Host paths for agent volumes
HOST_TEMPLATES_PATH=${HOST_TEMPLATES_PATH:-${REMOTE_DIR}/config/agent-templates}
TRINITY_DATA_PATH=${TRINITY_DATA_PATH:-${REMOTE_DIR}/trinity-data}
"

    # Write to remote
    echo "${ENV_CONTENT}" | gcloud compute ssh "${GCP_INSTANCE}" \
        --zone="${GCP_ZONE}" \
        --command="cat > ${REMOTE_DIR}/.env"

    echo ".env file created."
}

# Function to build base image
build_base_image() {
    echo "Building base agent image on remote..."
    gcloud compute ssh "${GCP_INSTANCE}" \
        --zone="${GCP_ZONE}" \
        --command="cd ${REMOTE_DIR} && sudo docker build -t trinity-agent-base:latest -f docker/base-image/Dockerfile docker/base-image/"
    echo "Base image built."
}

# Function to restart services
restart_services() {
    echo "Restarting services on ${GCP_INSTANCE}..."

    gcloud compute ssh "${GCP_INSTANCE}" \
        --zone="${GCP_ZONE}" \
        --command="cd ${REMOTE_DIR} && sudo docker compose -f docker-compose.prod.yml down && sudo docker compose -f docker-compose.prod.yml up -d --build"

    echo "Services restarted."
}

# Function to show status
show_status() {
    echo ""
    echo "Checking service status..."

    gcloud compute ssh "${GCP_INSTANCE}" \
        --zone="${GCP_ZONE}" \
        --command="cd ${REMOTE_DIR} && sudo docker compose -f docker-compose.prod.yml ps"
}

# Main deployment logic
case "${1:-full}" in
    sync)
        sync_files
        ;;
    restart)
        restart_services
        show_status
        ;;
    env)
        create_remote_env
        ;;
    status)
        show_status
        ;;
    base)
        build_base_image
        ;;
    full|*)
        sync_files
        create_remote_env
        build_base_image
        restart_services

        echo ""
        echo "Waiting for services to start..."
        sleep 10

        show_status

        echo ""
        echo "====================================="
        echo "Deployment Complete!"
        echo "====================================="
        echo ""
        if [ -n "${DOMAIN}" ] && [ "${DOMAIN}" != "your-domain.com" ]; then
            echo "Access your deployment at:"
            echo "  Web UI:  https://${DOMAIN}"
            echo "  API:     https://${DOMAIN}/api/docs"
            echo "  MCP:     http://${GCP_EXTERNAL_IP:-$DOMAIN}:${MCP_PORT:-8080}/mcp"
        else
            echo "Access your deployment at:"
            echo "  Web UI:  http://${GCP_EXTERNAL_IP}:${FRONTEND_PORT:-80}"
            echo "  API:     http://${GCP_EXTERNAL_IP}:${BACKEND_PORT:-8000}/docs"
        fi
        ;;
esac
