# Trinity Deployment Guide

This guide covers deploying Trinity to a production environment.

## Prerequisites

- Docker and Docker Compose v2+
- A server with at least 4 vCPU and 8 GB RAM (recommended: 8 vCPU, 32 GB RAM for multiple agents)
- Domain name (optional, for HTTPS)
- SSL certificate (Let's Encrypt recommended)

## Quick Start (Local Development)

```bash
# 1. Clone the repository
git clone https://github.com/abilityai/trinity.git
cd trinity

# 2. Copy and configure environment
cp .env.example .env
# Edit .env with your settings (see Configuration section)

# 3. Build the base agent image
./scripts/deploy/build-base-image.sh

# 4. Start all services
./scripts/deploy/start.sh

# 5. Access the platform
# Web UI: http://localhost:3000
# API Docs: http://localhost:8000/docs
```

## Configuration

### Required Environment Variables

Edit `.env` with these required settings:

```bash
# Security - REQUIRED (generate with: openssl rand -hex 32)
SECRET_KEY=your-secret-key-here

# Admin credentials for dev mode
ADMIN_USERNAME=admin
ADMIN_PASSWORD=your-secure-password

# Anthropic API Key - Required for agents
ANTHROPIC_API_KEY=sk-ant-your-api-key
```

### GitHub Templates (Optional)

To use GitHub-based agent templates (private repositories), add your GitHub Personal Access Token:

```bash
# GitHub PAT for cloning private template repos
# Get from: https://github.com/settings/tokens (classic token with 'repo' scope)
GITHUB_PAT=github_pat_xxxxx
```

**How it works:**
- On startup, the backend automatically uploads the PAT to Redis
- GitHub templates in `config.py` reference this credential
- When creating an agent from a GitHub template, the PAT is used to clone the repo

**Note:** The PAT is stored in Redis with a fixed credential ID (`github-pat-templates`). If you update the PAT in `.env`, restart the backend to sync it to Redis.

### Authentication Modes

Trinity supports two authentication modes:

#### Development Mode (Default)
Username/password login for local development:
```bash
DEV_MODE_ENABLED=true
```

#### Production Mode (Auth0)
OAuth authentication via Auth0:
```bash
DEV_MODE_ENABLED=false
AUTH0_DOMAIN=your-tenant.us.auth0.com
AUTH0_ALLOWED_DOMAIN=your-company.com  # Optional domain restriction
```

Frontend Auth0 variables (in `src/frontend/.env.local`):
```bash
VITE_AUTH0_DOMAIN=your-tenant.us.auth0.com
VITE_AUTH0_CLIENT_ID=your-client-id
VITE_AUTH0_ALLOWED_DOMAIN=your-company.com
```

## Production Deployment

### 1. Server Setup

Requirements:
- Linux server (Debian/Ubuntu recommended)
- Docker 24+ with Compose v2
- 100 GB+ disk space (agents can grow large)

### 2. Firewall Configuration

Open these ports:
| Port | Service |
|------|---------|
| 80 | HTTP (redirect to HTTPS) |
| 443 | HTTPS |
| 3000 | Frontend (or behind nginx) |
| 8000 | Backend API |
| 8080 | MCP Server |
| 2222-2262 | Agent SSH (optional) |

### 3. Deploy with Docker Compose

```bash
# Copy files to server
scp -r . your-server:~/trinity/

# SSH to server
ssh your-server

# Build and start
cd ~/trinity
cp .env.example .env
# Edit .env with production values
./scripts/deploy/build-base-image.sh
docker compose -f docker-compose.prod.yml up -d
```

### 4. SSL with nginx (Recommended)

Example nginx configuration:

```nginx
server {
    listen 80;
    server_name your-domain.com;
    return 301 https://$server_name$request_uri;
}

server {
    listen 443 ssl;
    server_name your-domain.com;

    ssl_certificate /etc/letsencrypt/live/your-domain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/your-domain.com/privkey.pem;

    # Frontend
    location / {
        proxy_pass http://127.0.0.1:3000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host $host;
    }

    # Backend API
    location /api/ {
        proxy_pass http://127.0.0.1:8000/api/;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_read_timeout 300s;
    }

    # WebSocket
    location /ws {
        proxy_pass http://127.0.0.1:8000/ws;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection 'upgrade';
    }
}
```

### 5. CORS Configuration

Add your production domain:
```bash
EXTRA_CORS_ORIGINS=https://your-domain.com,http://your-domain.com
```

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      Trinity Platform                        │
├─────────────────────────────────────────────────────────────┤
│  Frontend (Vue.js)  │  Backend (FastAPI)  │  MCP Server     │
│     Port 3000       │     Port 8000       │    Port 8080    │
├─────────────────────────────────────────────────────────────┤
│  Redis (secrets)    │  SQLite (data)      │  Audit Logger   │
│   Internal only     │   /data volume      │    Port 8001    │
├─────────────────────────────────────────────────────────────┤
│                    Agent Containers                          │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐                     │
│  │ Agent 1 │  │ Agent 2 │  │ Agent N │  ...                │
│  │ SSH:2222│  │ SSH:2223│  │ SSH:222N│                     │
│  └─────────┘  └─────────┘  └─────────┘                     │
└─────────────────────────────────────────────────────────────┘
```

## Data Persistence

| Data | Location | Backup Strategy |
|------|----------|-----------------|
| SQLite (users, agents) | `~/trinity-data/trinity.db` | Regular file backup |
| Redis (credentials) | Docker volume | Redis RDB snapshots |
| Agent workspaces | Docker volumes | Per-agent backup |

### Backup Script

```bash
# Backup database
./scripts/deploy/backup-database.sh ./backups/

# Restore from backup
./scripts/deploy/restore-database.sh ./backups/trinity_backup.db
```

## Troubleshooting

### View Logs

```bash
# All services
docker compose logs -f

# Specific service
docker compose logs -f backend
docker compose logs -f frontend

# Agent container
docker logs agent-myagent
```

### Common Issues

**Agent creation fails**
- Check if `trinity-agent-base` image exists: `docker images | grep trinity-agent-base`
- Rebuild: `./scripts/deploy/build-base-image.sh`

**Redis connection errors**
- Ensure Redis is running: `docker compose ps redis`
- Check Redis logs: `docker compose logs redis`

**Auth0 not working**
- Verify `DEV_MODE_ENABLED=false`
- Check Auth0 domain and client ID in frontend `.env.local`
- Verify callback URLs in Auth0 dashboard

## Cloud Deployment Options

Trinity can be deployed to any cloud provider.

---

### Google Cloud Platform (GCP)

We provide automated deployment scripts for GCP. These scripts use a configuration file that you create locally (not committed to git).

#### Step 1: Create Your Configuration

```bash
# Copy the template
cp deploy.config.example deploy.config

# Edit with your settings
nano deploy.config  # or your preferred editor
```

Required settings in `deploy.config`:
```bash
GCP_PROJECT="your-gcp-project-id"
GCP_ZONE="us-central1-a"
GCP_INSTANCE="your-vm-name"
DOMAIN="your-domain.com"
```

#### Step 2: Set Up Firewall Rules

```bash
./scripts/deploy/gcp-firewall.sh
```

This creates firewall rules for:
- Trinity services (frontend, backend, MCP server)
- Agent SSH ports

#### Step 3: Deploy to GCP

```bash
# Full deployment (sync files, create env, restart services)
./scripts/deploy/gcp-deploy.sh

# Or individual steps:
./scripts/deploy/gcp-deploy.sh sync      # Sync files only
./scripts/deploy/gcp-deploy.sh restart   # Restart services only
./scripts/deploy/gcp-deploy.sh status    # Check status
```

#### Step 4: Set Up SSL (Recommended)

On your GCP VM, install nginx and certbot:

```bash
sudo apt install nginx certbot python3-certbot-nginx
sudo certbot --nginx -d your-domain.com
```

#### Backup & Restore (GCP)

```bash
# Backup database from GCP to local
./scripts/deploy/backup-database.sh

# Restore database to GCP
./scripts/deploy/restore-database.sh ./backups/trinity_backup_20251201.db
```

---

### AWS
- EC2 instance with Docker
- RDS for PostgreSQL (with adapter)
- ElastiCache for Redis

### Azure
- Azure VM with Docker
- Azure Database for PostgreSQL
- Azure Cache for Redis

## Security Recommendations

1. **Never expose Redis externally** - Keep it internal only
2. **Use strong SECRET_KEY** - Generate with `openssl rand -hex 32`
3. **Enable Auth0 in production** - Don't use dev mode
4. **Regular backups** - Automate database backups
5. **Limit agent SSH access** - Use firewall rules
6. **Keep Docker updated** - Regular security patches
