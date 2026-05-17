---
name: deploy-status
description: Check health of production deployment including frontend, backend, MCP server, and audit logger.
allowed-tools: [Bash, Read]
user-invocable: true
automation: autonomous
---

# Deployment Status Check

Check health of production deployment.

## State Dependencies

| Source | Location | Read | Write | Description |
|--------|----------|------|-------|-------------|
| Local Config | `CLAUDE.local.md` | ✅ | | Deployment details |
| Frontend | `https://${PRODUCTION_URL}` | ✅ | | Frontend health |
| Backend API | `https://${PRODUCTION_URL}/api/health` | ✅ | | Backend health |
| MCP Server | `http://${SERVER_IP}:8007/mcp` | ✅ | | MCP health |
| Audit Logger | `http://${SERVER_IP}:8006/docs` | ✅ | | Logger health |
| VM Containers | GCP VM via SSH | ✅ | | Container status |

## Prerequisites

This command requires `CLAUDE.local.md` to be configured with your deployment details:
- Production URL
- Server IP
- GCP project and zone
- VM name

If `CLAUDE.local.md` doesn't exist, create it from `CLAUDE.local.md.example`.

## Process

### Step 1: Read Configuration

Read `CLAUDE.local.md` to get deployment configuration.

### Step 2: Check Frontend

```bash
curl -s -o /dev/null -w "%{http_code}" https://${PRODUCTION_URL}
```

### Step 3: Check Backend

```bash
curl -s https://${PRODUCTION_URL}/api/health | python3 -m json.tool
```

### Step 4: Check MCP Server

```bash
curl -s http://${SERVER_IP}:8007/mcp | head -20
```

### Step 5: Check Audit Logger

```bash
curl -s -o /dev/null -w "%{http_code}" http://${SERVER_IP}:8006/docs
```

### Step 6: Check Containers (if SSH available)

```bash
gcloud compute ssh ${VM_NAME} --zone=${GCP_ZONE} --command="docker ps --format 'table {{.Names}}\t{{.Status}}'"
```

### Step 7: Report Status

```
## Production Deployment Status

### Services
| Service | URL | Status |
|---------|-----|--------|
| Frontend | https://${PRODUCTION_URL} | ✅ 200 |
| Backend API | https://${PRODUCTION_URL}/api/ | ✅ Healthy |
| MCP Server | :8007/mcp | ✅ Running |
| Audit Logger | :8006/docs | ✅ Running |

### SSL Certificate
- Issuer: Let's Encrypt
- Expiry: [date from cert]
- Auto-renewal: ✅ Enabled

### Port Allocation
| Port | Service |
|------|---------|
| 3005 | Frontend |
| 8005 | Backend |
| 8006 | Audit Logger |
| 8007 | MCP Server |
| 2224-2242 | Agent SSH |

### Quick Actions
- Deploy: `./scripts/deploy/gcp-deploy.sh`
- Backup DB: `./scripts/deploy/backup-database.sh`
- View logs: `gcloud compute ssh ${VM_NAME} --command="docker-compose logs -f"`
```

## When to Use

- Before and after production deployments
- When users report production issues
- For daily health checks
- After SSL certificate renewals

## Troubleshooting

If services are down:
1. SSH to VM: `gcloud compute ssh ${VM_NAME} --zone=${GCP_ZONE}`
2. Check containers: `docker ps -a`
3. Check logs: `docker-compose -f docker-compose.prod.yml logs`
4. Restart services: `docker-compose -f docker-compose.prod.yml up -d`

## Completion Checklist

- [ ] CLAUDE.local.md read successfully
- [ ] Frontend health checked
- [ ] Backend API health checked
- [ ] MCP Server health checked
- [ ] Audit Logger health checked
- [ ] Container status retrieved (if SSH available)
- [ ] Status report generated
