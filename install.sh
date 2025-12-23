#!/bin/bash
#
# Trinity Deep Agent Platform - One-Line Installer
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/abilityai/trinity/main/install.sh | bash
#
# Or if you've already cloned the repo:
#   ./install.sh
#

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

print_banner() {
    echo -e "${BLUE}"
    echo "╔════════════════════════════════════════════════════════════╗"
    echo "║                                                            ║"
    echo "║   ████████╗██████╗ ██╗███╗   ██╗██╗████████╗██╗   ██╗     ║"
    echo "║   ╚══██╔══╝██╔══██╗██║████╗  ██║██║╚══██╔══╝╚██╗ ██╔╝     ║"
    echo "║      ██║   ██████╔╝██║██╔██╗ ██║██║   ██║    ╚████╔╝      ║"
    echo "║      ██║   ██╔══██╗██║██║╚██╗██║██║   ██║     ╚██╔╝       ║"
    echo "║      ██║   ██║  ██║██║██║ ╚████║██║   ██║      ██║        ║"
    echo "║      ╚═╝   ╚═╝  ╚═╝╚═╝╚═╝  ╚═══╝╚═╝   ╚═╝      ╚═╝        ║"
    echo "║                                                            ║"
    echo "║          Deep Agent Orchestration Platform                 ║"
    echo "║                                                            ║"
    echo "╚════════════════════════════════════════════════════════════╝"
    echo -e "${NC}"
}

log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[OK]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

check_command() {
    if command -v "$1" &> /dev/null; then
        log_success "$1 is installed"
        return 0
    else
        log_error "$1 is not installed"
        return 1
    fi
}

generate_secret_key() {
    # Generate a secure random key
    if command -v openssl &> /dev/null; then
        openssl rand -hex 32
    else
        # Fallback for systems without openssl
        head -c 32 /dev/urandom | xxd -p | tr -d '\n'
    fi
}

generate_password() {
    # Generate a random password (16 chars)
    if command -v openssl &> /dev/null; then
        openssl rand -base64 16 | tr -dc 'a-zA-Z0-9' | head -c 16
    else
        head -c 16 /dev/urandom | xxd -p | head -c 16
    fi
}

# =============================================================================
# Main Installation
# =============================================================================

print_banner

echo ""
log_info "Starting Trinity installation..."
echo ""

# -----------------------------------------------------------------------------
# Step 1: Check prerequisites
# -----------------------------------------------------------------------------
log_info "Checking prerequisites..."

MISSING_DEPS=0

if ! check_command "docker"; then
    MISSING_DEPS=1
fi

if ! check_command "git"; then
    MISSING_DEPS=1
fi

# Check Docker Compose (v2 is bundled with Docker)
if docker compose version &> /dev/null; then
    log_success "Docker Compose v2 is available"
else
    log_error "Docker Compose is not available"
    MISSING_DEPS=1
fi

# Check if Docker daemon is running
if docker info &> /dev/null; then
    log_success "Docker daemon is running"
else
    log_error "Docker daemon is not running. Please start Docker first."
    MISSING_DEPS=1
fi

if [ $MISSING_DEPS -eq 1 ]; then
    echo ""
    log_error "Missing dependencies. Please install them first:"
    echo "  - Docker: https://docs.docker.com/get-docker/"
    echo ""
    exit 1
fi

echo ""

# -----------------------------------------------------------------------------
# Step 2: Clone or use existing repo
# -----------------------------------------------------------------------------
TRINITY_DIR=""

# Check if we're already in the trinity directory
if [ -f "docker-compose.yml" ] && [ -d "src/backend" ]; then
    TRINITY_DIR="$(pwd)"
    log_info "Using existing Trinity installation at $TRINITY_DIR"
else
    # Clone the repository
    TRINITY_DIR="$HOME/trinity"

    if [ -d "$TRINITY_DIR" ]; then
        log_warn "Directory $TRINITY_DIR already exists"
        read -p "Remove and re-clone? [y/N] " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            rm -rf "$TRINITY_DIR"
        else
            log_info "Using existing directory"
        fi
    fi

    if [ ! -d "$TRINITY_DIR" ]; then
        log_info "Cloning Trinity repository..."
        git clone https://github.com/abilityai/trinity.git "$TRINITY_DIR"
        log_success "Repository cloned to $TRINITY_DIR"
    fi
fi

cd "$TRINITY_DIR"

# -----------------------------------------------------------------------------
# Step 3: Create .env file
# -----------------------------------------------------------------------------
log_info "Configuring environment..."

if [ -f ".env" ]; then
    log_warn ".env file already exists, backing up to .env.backup"
    cp .env .env.backup
fi

if [ ! -f ".env.example" ]; then
    log_error ".env.example not found. Repository may be corrupted."
    exit 1
fi

# Copy example and configure
cp .env.example .env

# Generate secure values
SECRET_KEY=$(generate_secret_key)
ADMIN_PASSWORD=$(generate_password)

# Update .env with generated values
if [[ "$OSTYPE" == "darwin"* ]]; then
    # macOS sed syntax
    sed -i '' "s/^SECRET_KEY=.*/SECRET_KEY=$SECRET_KEY/" .env
    sed -i '' "s/^ADMIN_PASSWORD=.*/ADMIN_PASSWORD=$ADMIN_PASSWORD/" .env
else
    # Linux sed syntax
    sed -i "s/^SECRET_KEY=.*/SECRET_KEY=$SECRET_KEY/" .env
    sed -i "s/^ADMIN_PASSWORD=.*/ADMIN_PASSWORD=$ADMIN_PASSWORD/" .env
fi

log_success "Environment configured"
echo ""
echo -e "  ${YELLOW}Admin Credentials (save these!):${NC}"
echo -e "    Username: ${GREEN}admin${NC}"
echo -e "    Password: ${GREEN}$ADMIN_PASSWORD${NC}"
echo ""

# -----------------------------------------------------------------------------
# Step 4: Build base agent image
# -----------------------------------------------------------------------------
log_info "Building Trinity agent base image (this may take a few minutes)..."

if [ -f "scripts/deploy/build-base-image.sh" ]; then
    chmod +x scripts/deploy/build-base-image.sh
    ./scripts/deploy/build-base-image.sh
    log_success "Base image built"
else
    log_error "build-base-image.sh not found"
    exit 1
fi

# -----------------------------------------------------------------------------
# Step 5: Start services
# -----------------------------------------------------------------------------
log_info "Starting Trinity services..."

docker compose up -d

# Wait for services to be healthy
log_info "Waiting for services to start..."
sleep 5

# Check if services are running
if docker compose ps | grep -q "running"; then
    log_success "Services started"
else
    log_error "Some services failed to start. Check 'docker compose logs' for details."
    exit 1
fi

# -----------------------------------------------------------------------------
# Done!
# -----------------------------------------------------------------------------
echo ""
echo -e "${GREEN}╔════════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║                                                            ║${NC}"
echo -e "${GREEN}║              Trinity installed successfully!               ║${NC}"
echo -e "${GREEN}║                                                            ║${NC}"
echo -e "${GREEN}╚════════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  ${BLUE}Web UI:${NC}      http://localhost:3000"
echo -e "  ${BLUE}API Docs:${NC}    http://localhost:8000/docs"
echo ""
echo -e "  ${YELLOW}Next Steps:${NC}"
echo -e "    1. Open http://localhost:3000 in your browser"
echo -e "    2. Login with admin / $ADMIN_PASSWORD"
echo -e "    3. Go to Settings and add your Anthropic API key"
echo -e "    4. Create your first agent!"
echo ""
echo -e "  ${BLUE}Useful Commands:${NC}"
echo -e "    cd $TRINITY_DIR"
echo -e "    docker compose logs -f      # View logs"
echo -e "    docker compose down         # Stop services"
echo -e "    docker compose up -d        # Start services"
echo ""
