# Trinity Scheduler Service Dockerfile
# Standalone scheduler for agent task execution

FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
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
