# Trinity Scheduler Service Dockerfile
# Standalone scheduler for agent task execution

FROM python:3.14-slim

# Set working directory
WORKDIR /app

# Install system dependencies
#
# `tzdata-legacy` carries the IANA *backward-compatibility links* (#1823). Debian
# 13 "trixie" split them out of `tzdata`, and this image is `FROM python:3.13-slim`
# with no codename pin — so it silently migrated bookworm→trixie and lost
# `/usr/share/zoneinfo/Europe/Kiev` (and every other legacy alias). This is the
# image that actually registers the CronTrigger: without the links a stored
# legacy-alias schedule can never register, so it never fires and its
# `next_run_at` freezes. Do NOT drop this when slimming the image —
# `tests/unit/test_1823_tz_capability_parity.py` fails if an image that resolves
# timezones declares no tz-data source.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    tzdata-legacy \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for caching
COPY docker/scheduler/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy scheduler source code
COPY src/scheduler /app/scheduler

# Set Python path
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8001/health || exit 1

# Default environment variables
ENV DATABASE_PATH=/data/trinity.db
ENV REDIS_URL=redis://redis:6379
ENV HEALTH_PORT=8001
ENV LOG_LEVEL=INFO

# Run as non-root (CSO hardening, issue #874). UID must match backend's UID
# because both services share /data (SQLite trinity.db via the trinity-data
# bind/named volume) — misalignment would leave the WAL/journal unwritable
# by one of them.
RUN groupadd --gid 1000 trinity && \
    useradd --uid 1000 --gid trinity --home-dir /app --shell /bin/bash trinity && \
    mkdir -p /data && \
    chown -R trinity:trinity /app /data

USER trinity

# Run the scheduler
CMD ["python", "-m", "scheduler.main"]
