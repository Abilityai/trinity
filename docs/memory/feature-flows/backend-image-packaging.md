# Feature Flow: Backend Prod-Image Packaging Guard (#1033)

> **Status**: Shipped 2026-06-02. Build/CI integrity guard — no UI, no API, no
> DB. Closes the source→image packaging gap that crash-looped the prod backend.

## Problem

Commit `0c671158` (#526, dispatch circuit breaker) added a new **top-level**
module `src/backend/redis_breaker_util.py` — imported by
`services/agent_client.py` and `services/dispatch_breaker.py` — but never added
it to the **enumerated** `COPY` list in `docker/backend/Dockerfile`. The module
was therefore silently absent from the baked prod image, and the backend
crash-looped on boot:

```
ModuleNotFoundError: No module named 'redis_breaker_util'
```

This is a *packaging* defect, not a source defect: the source tree was correct;
only the image was incomplete.

## Why no existing CI caught it

| CI surface | Why it was blind |
|------------|------------------|
| `container-security.yml` | Boots the **dev** compose via `start.sh`, which bind-mounts `./src/backend:/app`. The live source masks any missing `COPY`, so a packaging defect is structurally invisible there. |
| `backend-unit-test.yml` | Runs `pytest` against **source**. Blind to image packaging entirely. |
| `docker-compose.prod.yml` | Does **not** bind-mount `src/backend`, so the bug only bit on real prod deploys — after merge. |

The gap: nothing exercised the **baked prod image with no dev bind-mount**.

## The fix — two parts

### 1. Dockerfile: enumerate → glob (`docker/backend/Dockerfile`)

The seven hand-listed top-level `COPY` lines collapse to one glob so a new
top-level module can never be dropped again:

```dockerfile
# Copy all top-level backend modules. Globbed (not enumerated) so a new
# top-level module can never be silently dropped from the prod image again
# (#1033: redis_breaker_util.py ...). Subdirectories are copied wholesale below.
COPY ../../src/backend/*.py /app/
```

The glob bakes every top-level module — currently `config.py`, `database.py`,
`db_models.py`, `dependencies.py`, `logging_config.py`, `main.py`, `models.py`,
`redis_breaker_util.py`. Subdirectories (`routers/`, `services/`, `adapters/`,
`utils/`, `db/`, `canary/`) are already copied wholesale, so only the top-level
list was at risk. The downstream `chmod -R 644 /app/*.py` already matched the
glob set, so no permission change was needed.

### 2. Test relocation (`src/backend/staging-acceptance.py` → `tests/integration/staging_acceptance.py`)

`*.py` now sweeps *anything* sitting at `src/backend/` top level into the prod
image. A stray hyphenated test file (`staging-acceptance.py`, a #678
live-stack acceptance test — not importable as a module and not production code)
was moved out to `tests/integration/staging_acceptance.py` (underscore name, now
import-clean) so the glob only ever bakes real runtime modules. The
2026-06-02 refactor-audit report was updated to point at the new path.

## Recurrence guard — `backend-image-smoke.yml` CI job

`.github/workflows/backend-image-smoke.yml` exercises the **baked prod image**
(no dev bind-mount), so it sees packaging *and* lifespan/migration/startup
defects. Deliberately a **separate** workflow from `container-security.yml`
(which boots the dev stack and cannot see image-packaging bugs).

**Triggers** (`push` to `dev`/`main` + `pull_request`), path-filtered to the
protected surface — mirrors the path-filter-not-label-gate rule for backend
infra guards:

```
src/backend/**
docker/backend/Dockerfile
docker-compose.prod.yml
config/vector.yaml
.github/workflows/backend-image-smoke.yml
```

**Least-privilege**: `permissions: contents: read` only (checkout; no PR
comments / no security-events). `COMPOSE_PROJECT_NAME=trinity-smoke` pins the
namespace so build / module-check / `up` / `down` share one project and the
built image gets the deterministic tag `trinity-smoke-backend`.

**Step sequence** (escalating signal, cheapest first):

1. **Generate boot secrets (`.env`)** — `docker-compose.prod.yml` uses
   `${VAR:?...}` for `ADMIN_PASSWORD`/`REDIS_PASSWORD`/`REDIS_BACKEND_PASSWORD`
   and refuses to render without them. All values are per-run random (no real
   secrets). `TRINITY_DATA_PATH=./trinity-data` is pinned so the chown step
   targets the right dir.
2. **Prepare data path ownership (UID 1000)** — backend runs non-root (#874) and
   writes `/data/trinity.db` at import (`database.py` → `init_database`); the
   bind-mounted host dir is `chown`ed to `1000:1000`. Mirrors `start.sh`.
3. **Detect docker GID** — backend joins the host docker group via
   `group_add: ${DOCKER_GID:-999}`; the runner's actual GID is detected rather
   than trusting `999`.
4. **Build prod backend image** — via `docker compose -f docker-compose.prod.yml
   build backend` so build context + args exactly match prod (no hand-rolled
   `docker build` drift).
5. **Assert new top-level module is baked in** — `docker run … ls -l
   /app/redis_breaker_util.py`. The exact module #1033 dropped.
6. **Import-chain fast check** — `python -c "import redis_breaker_util; import
   services.agent_client; import main; print('IMPORT OK')"`. Reproduces the
   exact crash-loop chain. `REDIS_URL` must carry credentials or `config.py`
   raises at import; no live Redis is contacted (network I/O is deferred to the
   lifespan handler).
7. **Boot prod image and wait for `/health`** — `up -d redis vector backend`
   (hard `depends_on` for both in prod compose), polls
   `http://localhost:8000/health` for ~120s. Subsumes the import check and
   additionally catches lifespan / migration / Redis-wiring startup failures.
8. **Collect + upload logs on failure** (`if: failure()`, 14-day retention) and
   **tear down** (`if: always()`, `down -v --remove-orphans`).

## Relationship to `/verify-local`

This CI job is the enforced server-side guard for the #1033 bug class. Its local
counterpart is the personal `/verify-local` skill, which builds + boots the real
prod images on the developer's machine before push — same goal (catch
source→image packaging/boot defects that source-only CI misses), different stage.

## Verification

- `ls src/backend/*.py` ⇒ 8 modules, all swept by the new glob (was 7 enumerated,
  `redis_breaker_util.py` missing).
- `docker compose -f docker-compose.prod.yml build backend` then
  `docker run --rm trinity-smoke-backend python -c "import redis_breaker_util;
  import services.agent_client; import main"` ⇒ `IMPORT OK`.
- `curl -fsS http://localhost:8000/health` returns 200 after `up -d redis vector
  backend`.

## Related Flows

- [dispatch-circuit-breaker.md](dispatch-circuit-breaker.md) — introduced
  `redis_breaker_util.py` (the dropped module) and documents why it is
  top-level (IRON RULE R1), which is precisely what the enumerated `COPY` list
  failed to keep up with.
- [async-docker-operations.md](async-docker-operations.md) — runtime Docker SDK
  wrappers (this flow is build-time, not runtime).
