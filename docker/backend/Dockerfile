FROM python:3.13-slim

WORKDIR /app

# Unbuffered stdout/stderr — otherwise Python block-buffers print()/log output
# to the Docker pipe (~8KB) and lifespan output (notably the first-time setup
# token in main.py) never reaches `docker logs`. Matches docker/scheduler/Dockerfile.
ENV PYTHONUNBUFFERED=1

# Install curl for healthcheck, git for GitHub cloning, libmagic for file type detection
RUN apt-get update && apt-get install -y --no-install-recommends curl git libmagic1 ffmpeg && rm -rf /var/lib/apt/lists/*

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
    pyyaml==6.0.2 \
    sqlalchemy==2.0.36 \
    alembic==1.18.4 \
    psycopg2-binary==2.9.10 \
    docker==7.1.0 \
    aiofiles==24.1.0 \
    apscheduler==3.11.0 \
    croniter==5.0.1 \
    pytz==2024.2 \
    psutil==6.1.1 \
    payments-py==1.2.1 \
    Pillow==11.1.0 \
    google-genai==1.12.1 \
    "slack_sdk[socket-mode]==3.41.0" \
    aiohttp \
    slackify-markdown \
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

