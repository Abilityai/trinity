---
name: agent-status
description: Check the status of all Trinity agent containers including infrastructure health.
allowed-tools: [Bash]
user-invocable: true
automation: autonomous
---

# Agent Status Check

Check the status of all Trinity agent containers.

## State Dependencies

| Source | Location | Read | Write | Description |
|--------|----------|------|-------|-------------|
| Docker Containers | Docker socket | ✅ | | Agent containers |
| Backend Health | `http://localhost:8000/api/health` | ✅ | | Backend status |
| Agent List | `http://localhost:8000/api/agents` | ✅ | | Agent metadata |
| Redis | `trinity-redis` container | ✅ | | Redis connectivity |

## Process

### Step 1: List Running Containers

```bash
docker ps --filter "label=trinity.platform=agent" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
```

### Step 2: Check Backend Health

```bash
curl -s http://localhost:8000/api/health | python3 -m json.tool
```

### Step 3: List Agents via API

```bash
curl -s http://localhost:8000/api/agents | python3 -m json.tool
```

### Step 4: Check Redis Connectivity

```bash
docker exec trinity-redis redis-cli ping
```

### Step 5: Report Status

```
## Agent Status Report

### Infrastructure
- Backend: ✅ Running / ❌ Down
- Redis: ✅ Connected / ❌ Down
- Audit Logger: ✅ Running / ❌ Down

### Agents
| Name | Status | Uptime | Template |
|------|--------|--------|----------|
| agent-1 | running | 2h 15m | github:Org/repo |
| agent-2 | stopped | - | local-template |

### Quick Actions
- Start agent: `curl -X POST http://localhost:8000/api/agents/{name}/start`
- Stop agent: `curl -X POST http://localhost:8000/api/agents/{name}/stop`
- View logs: `docker logs agent-{name}`
```

## When to Use

- Before starting development work
- When debugging agent issues
- To verify deployment health
- After infrastructure changes

## Troubleshooting

If agents not showing:
1. Check Docker socket: `ls -la /var/run/docker.sock`
2. Check backend logs: `docker-compose logs backend`
3. Verify network: `docker network ls | grep trinity`

## Completion Checklist

- [ ] Docker containers listed
- [ ] Backend health verified
- [ ] Agent list retrieved
- [ ] Redis connectivity confirmed
- [ ] Status report generated
