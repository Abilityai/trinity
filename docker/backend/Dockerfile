FROM python:3.14-slim

WORKDIR /app

# Unbuffered stdout/stderr — otherwise Python block-buffers print()/log output
# to the Docker pipe (~8KB) and lifespan output (notably the first-time setup
# token in main.py) never reaches `docker logs`. Matches docker/scheduler/Dockerfile.
ENV PYTHONUNBUFFERED=1

# Install curl for healthcheck, git for GitHub cloning, libmagic for file type detection
#
# `tzdata-legacy` carries the IANA *backward-compatibility links* (#1823). Debian
# 13 "trixie" split them out of `tzdata`, and this image is `FROM python:3.13-slim`
# with no codename pin — so it silently migrated bookworm→trixie and lost
# `/usr/share/zoneinfo/Europe/Kiev` (and every other legacy alias) without anyone
# changing a line. `zoneinfo.ZoneInfo("Europe/Kiev")` then raises
# ZoneInfoNotFoundError inside APScheduler's `astimezone()`, which is how a
# perfectly ordinary schedule create started 500ing. Do NOT drop this when
# slimming the image — `tests/unit/test_1823_tz_capability_parity.py` fails if
# an image that resolves timezones declares no tz-data source.
# `postgresql-client-17` (#2216) carries pg_dump for the nightly PostgreSQL
# backup arm (services/db_backup_service.py). MAJOR-PINNED deliberately, not
# the floating `postgresql-client` meta-package: this image already migrated
# bookworm→trixie silently once (#1823, the tzdata split above), and an
# unpinned client's major could change under an unrelated rebuild — breaking
# every PG install's backups at once. pg_dump handles servers ≤ its own major
# (bundled dev PG is 16); a future server-major above 17 fails LOUDLY
# (non-zero exit → operator alarm), never silently.
RUN apt-get update && apt-get install -y --no-install-recommends curl git libmagic1 ffmpeg tzdata-legacy postgresql-client-17 && rm -rf /var/lib/apt/lists/*

# Build-time provenance (#926). Each arg is forwarded by docker-compose
# `backend.build.args` from shell vars set by `scripts/deploy/start.sh`.
# Re-exported as ENV so the running container reads them via os.getenv()
# and `GET /api/version` can return commit/branch/build-date for the
# image actually in flight. Defaults to "unknown" so local builds and
# operators who skip start.sh still get a well-typed response.
ARG VERSION=unknown
ARG GIT_COMMIT=unknown
ARG GIT_COMMIT_SUBJECT=unknown
ARG GIT_COMMIT_TIMESTAMP=unknown
ARG GIT_BRANCH=unknown
ARG BUILD_DATE=unknown
ENV VERSION=${VERSION} \
    GIT_COMMIT=${GIT_COMMIT} \
    GIT_COMMIT_SUBJECT=${GIT_COMMIT_SUBJECT} \
    GIT_COMMIT_TIMESTAMP=${GIT_COMMIT_TIMESTAMP} \
    GIT_BRANCH=${GIT_BRANCH} \
    BUILD_DATE=${BUILD_DATE}

# `idna` is pinned explicitly even though httpx already pulls it in:
# services/setup_url_display.py canonicalises author-supplied `setup_url`
# hosts with UTS-46 nontransitional IDNA (browser semantics — the stdlib
# `str.encode("idna")` codec is IDNA2003 and disagrees on exactly the
# deviation set that matters, e.g. `fass.de` vs `xn--fa-hia.de`). A
# transitive pin is one dependency resolution away from vanishing, and the
# #1033 class of bug is invisible to /verify-local because the source
# imports fine on the host. (trinity-enterprise#127)
#
# cryptography is pinned explicitly: the 49.x line's bundled OpenSSL triggers a
# SIGILL (illegal instruction) when loaded in the same process as grpcio's
# BoringSSL (pulled in transitively by the OTLP gRPC exporter) on
# python:3.13-slim. Each imports fine alone; loaded together they crash on
# `import main`. 44.x is the last compatible line — verified by booting the
# backend end-to-end. Do not unpin without re-verifying startup.
RUN pip install --no-cache-dir \
    fastapi==0.115.6 \
    uvicorn[standard]==0.37.0 \
    "pydantic[email]>=2.10.4,<3" \
    python-jose[cryptography]==3.3.0 \
    cryptography==44.0.1 \
    passlib[bcrypt]==1.7.4 \
    bcrypt==4.2.1 \
    python-multipart==0.0.20 \
    websockets==14.1 \
    redis==5.2.1 \
    httpx==0.28.1 \
    idna==3.10 \
    pyyaml==6.0.2 \
    sqlalchemy==2.0.36 \
    alembic==1.18.4 \
    psycopg2-binary==2.9.10 \
    docker==7.1.0 \
    aiofiles==24.1.0 \
    apscheduler==3.11.0 \
    croniter==5.0.1 \
    pytz==2024.2 \
    # #1823 portability floor for the tz database. `zoneinfo` reads the SYSTEM
    # database first and falls back to this wheel only for keys the system
    # lacks (measured: a key present on TZPATH always wins), so with
    # `tzdata-legacy` installed above this is inert in production — it exists so
    # the capability survives another silent base-tag migration and holds on a
    # non-Debian base. Pinned exactly per this file's convention;
    # tests/requirements-test.txt deliberately keeps a floor instead (#1771 —
    # an exact pin there would freeze a stale IANA database). The staleness
    # hazard that motivates the floor there does not bite here precisely
    # because the wheel is inert while `tzdata-legacy` supplies the system db;
    # bump this pin with the rest of the file.
    tzdata==2026.3 \
    psutil==6.1.1 \
    payments-py==1.2.1 \
    Pillow==11.1.0 \
    # #1536 report export. Both are pure-Python wheels — no system libraries, so
    # the image build is unchanged beyond these two lines (WeasyPrint was
    # rejected for exactly that reason: cairo/pango would have made a PDF button
    # a container-build concern). The export endpoint degrades to 503 when they
    # are absent, so an instance that upgrades code without rebuilding the image
    # (#1814) gets a clear message instead of a 500.
    openpyxl==3.1.5 \
    reportlab==4.2.5 \
    google-genai==1.63.0 \
    "slack_sdk[socket-mode]==3.41.0" \
    aiohttp==3.14.3 \
    slackify-markdown==0.2.4 \
    tenacity==9.0.0 \
    opentelemetry-api==1.29.0 \
    opentelemetry-sdk==1.29.0 \
    opentelemetry-exporter-otlp==1.29.0 \
    opentelemetry-instrumentation-fastapi==0.50b0 \
    opentelemetry-instrumentation-httpx==0.50b0 \
    opentelemetry-instrumentation-redis==0.50b0 \
    python-magic==0.4.27 \
    pyotp==2.9.0 \
    twilio==9.10.5 \
    "audioop-lts; python_version >= '3.13'"

# Copy all top-level backend modules. Globbed (not enumerated) so a new
# top-level module can never be silently dropped from the prod image again
# (#1033: redis_breaker_util.py was added but never added to the explicit
# COPY list, crash-looping the backend on boot). Subdirectories are copied
# wholesale below.
COPY ../../src/backend/*.py /app/

# Copy module directories
COPY ../../src/backend/routers /app/routers/
COPY ../../src/backend/services /app/services/
COPY ../../src/backend/adapters /app/adapters/
COPY ../../src/backend/utils /app/utils/
COPY ../../src/backend/db /app/db/
COPY ../../src/backend/canary /app/canary/
# Workspace / client portal (ent#356). A new top-level backend package must be
# listed here or the image builds fine and the app dies at import — `main.py`
# does `from client_portal.router import ...` and the container has no such
# module (#1033's class). `prod-image-smoke` is what catches it; source-only CI
# cannot, because the source tree always has the directory.
COPY ../../src/backend/client_portal /app/client_portal/
# Multi-agent rooms (ent#443) — same rule, one module later. `main.py` imports
# `shared_sessions.router` unconditionally now, so an image without this package
# builds clean and dies at import with ModuleNotFoundError. That is exactly what
# `prod-image-smoke` caught on the PR that added it.
COPY ../../src/backend/shared_sessions /app/shared_sessions/

# Alembic migration scripts (#1183). db/alembic_runner.py points Alembic's
# script_location at /app/migrations at runtime, so the directory MUST ship in
# the image — the COPY *.py glob above only catches top-level modules, not this
# subdir (same #1033 packaging-gap class). Without it, a PostgreSQL deploy
# crash-loops in init_database() -> upgrade_to_head() (no script directory).
# alembic.ini is for CLI/autogenerate use (the runner builds its Config
# programmatically); copied so `alembic` works inside the container for ops.
COPY ../../src/backend/migrations /app/migrations/
COPY ../../src/backend/alembic.ini /app/alembic.ini

# Copy configuration files (documentation, templates)
COPY ../../config/process-docs /app/config/process-docs/
COPY ../../config/process-templates /app/config/process-templates/
# Bundled system manifests — the default first-run starter fleet lives here
# (trinity-enterprise#124). Dev compose bind-mounts ./config/manifests over
# this (the ./src/backend:/app mount shadows image COPYs); prod relies on it.
COPY ../../config/manifests /app/config/manifests/

# Platform version file — read by /api/version (#958). The dev compose also
# bind-mounts ./VERSION:/app/VERSION:ro for hot edits, but prod compose
# doesn't, so without this COPY the version field falls back to "unknown".
COPY ../../VERSION /app/VERSION

# Ensure all files are readable
RUN chmod -R 644 /app/*.py /app/alembic.ini && \
    chmod -R 755 /app/routers /app/services /app/adapters /app/utils /app/db /app/canary /app/migrations && \
    find /app/routers /app/services /app/adapters /app/utils /app/db /app/canary /app/migrations -name "*.py" -exec chmod 644 {} \;

# Run as non-root (CSO hardening, issue #874). UID 1000 must match scheduler
# because both services share /data (SQLite trinity.db). /agent-configs is
# included because services/agent_service/deploy.py writes templates there;
# a root-owned mount point would trigger the silent ./config/agent-templates
# fallback and templates would vanish on restart.
RUN groupadd --gid 1000 trinity && \
    useradd --uid 1000 --gid trinity --home-dir /app --shell /bin/bash trinity && \
    mkdir -p /data /agent-configs && \
    chown -R trinity:trinity /app /data /agent-configs

USER trinity

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--no-server-header", "--proxy-headers", "--forwarded-allow-ips=*"]

