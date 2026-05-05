# Migration: Redis authentication & network split (Issue #589)

**Status:** required for any deploy past commit `affd1f57`.
**Severity:** BREAKING — operator action required.

---

## What changed

1. **Redis now requires authentication.** `requirepass` + per-user ACL
   (`backend`, `scheduler`, `default`). Unauthenticated connections are
   rejected with `NOAUTH`.
2. **Two passwords by design.** `REDIS_PASSWORD` (admin) and
   `REDIS_BACKEND_PASSWORD` (runtime). Leaking the runtime password from
   a compromised platform container does NOT grant admin (FLUSHALL,
   CONFIG, SHUTDOWN).
3. **Network segmentation.** A new `trinity-platform-network`
   (172.29.0.0/16) hosts Redis, scheduler, and Vector. The existing
   `trinity-agent-network` (172.28.0.0/16) hosts agents and frontend.
   Backend, mcp-server, otel-collector, and (in prod) cloudflared
   straddle both. Agents have no route to Redis at all.
4. **Backend and scheduler refuse to start without a credentialed
   `REDIS_URL`.** `src/backend/config.py` and `src/scheduler/config.py`
   raise on import.

## Fresh installs (no existing data)

`./scripts/deploy/start.sh` auto-generates both passwords into `.env`
when the `redis-data` volume does not yet exist. No operator action
required.

## Upgrading an existing deployment

Re-keying a populated Redis will lock the backend out of its own data,
so this must be done while Redis is stopped.

```bash
# 1. Stop the stack and detach legacy agent containers from the
#    pre-split trinity-agent-network. (--remove-orphans is required
#    because stopped agent containers stay attached to whatever
#    network they had at create time.)
docker compose -f docker-compose.yml down --remove-orphans

# 2. Detach Trinity-managed agent containers from the old network.
#    Compose-managed services get fresh network refs on recreation,
#    but agent containers are created via the Docker SDK outside
#    compose and store the network's UUID, not its name. Once the
#    network is removed (step 3), any later `docker start <agent>`
#    fails with "network <uuid> not found". Disconnecting first
#    forces re-attachment to the new network on next start.
for c in $(docker ps -aq --filter "label=trinity.platform=agent"); do
    docker network disconnect trinity-agent-network "$c" 2>/dev/null || true
done

# 3. Remove the pre-split agent network. The new compose declares it
#    with different metadata; Docker refuses to re-use a network with
#    conflicting labels/subnets.
docker network rm trinity-agent-network 2>/dev/null || true

# 4. Add both passwords to .env.
cat <<EOF >> .env
REDIS_PASSWORD=$(openssl rand -hex 24)
REDIS_BACKEND_PASSWORD=$(openssl rand -hex 24)
EOF

# 5. Start the stack. Networks and services are recreated with the
#    new layout; Redis loads the ACL on first boot.
./scripts/deploy/start.sh
```

If `redis-data` already had data and you need to keep it: that data is
still there after restart — the new ACL only changes who can read it,
not what's stored. The `default` user with `REDIS_PASSWORD` retains
full access for ad-hoc ops.

If you want to start from a clean Redis instead, also remove the
volume **before** step 5:

```bash
docker volume rm $(basename "$PWD")_redis-data 2>/dev/null \
  || docker volume rm redis-data 2>/dev/null || true
```

## Production (`docker-compose.prod.yml`)

Same procedure, with `-f docker-compose.prod.yml` on every `docker
compose` call. Add `--profile tunnel` if cloudflared is in use.

## Verification

After the stack is up:

```bash
# Redis healthy:
docker inspect --format '{{.State.Health.Status}}' trinity-redis
# → healthy

# backend ACL user works:
docker exec trinity-redis redis-cli --user backend \
    -a "$REDIS_BACKEND_PASSWORD" --no-auth-warning PING
# → PONG

# Unauth rejected:
docker exec trinity-redis redis-cli PING
# → NOAUTH Authentication required.

# Agent can't reach Redis:
docker run --rm --network trinity-agent-network alpine:3.19 \
    sh -c 'nc -z -w 2 redis 6379 && echo REACHABLE || echo BLOCKED'
# → BLOCKED
```

## Rollback

If something is wrong and you need the old (unauth) Redis back, check
out the commit before `affd1f57`. Both passwords stay in `.env` — they
are ignored by the old compose. Issue #589 will reopen.
